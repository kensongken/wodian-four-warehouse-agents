import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { lstat, mkdir, readFile, realpath, rm, stat } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

import {
  CaptureEnvelopeV1Schema,
  ContentDemandSchema,
  ContentOpportunitySchema,
  CustomerDraftFeedbackSchema,
  DraftPackageSchema,
  EditBlueprintSchema,
  ExternalEditHandoffSchema,
  ExternalEditReturnCandidateSchema,
  FeedbackEventSchema,
  StudioDistributionEventSchema,
  MaterialCardSchema,
  MaterialInventorySchema,
  MatchAssessmentSchema,
  RevisionProposalSchema,
  STUDIO_INPUT_LIMITS,
  StudioCapabilitySchema,
  StudioCaptionCueSchema,
  StudioCaptionStyleSchema,
  StudioCreativeBriefSchema,
  type ContentDemand,
  type ContentOpportunity,
  type CustomerDraftFeedback,
  type DraftPackage,
  type ExternalEditHandoff,
  type ExternalEditReturnCandidate,
  type EditBlueprint,
  type FeedbackEvent,
  type StudioDistributionEvent,
  type GapList,
  type MaterialCard,
  type MaterialInventory,
  type MatchAssessment,
  type ModelAnalysisReceiptV1,
  type RevisionProposal,
  type StudioCapability,
  type StudioCaptionCue,
  type StudioCaptionStyle,
  type StudioCreativeBrief,
  type StudioDraftVisualEvidence,
  type StudioMediaAsset,
} from "@wearable/contracts";

import { CustomerMaterialLibrary } from "./customer-material-library";
import {
  OFFLINE_STUDIO_CAPABILITY,
  studioProviderCapability,
  type CaptureEnvelopeAdapter,
  type EditorialDecisionAdapter,
  type MediaUnderstandingAdapter,
  type OfflineAnalysisFixture,
  type StudioModelProviderIdentity,
  type StudioEditorialBeatEvidence,
  type StudioEditorialMusicTrack,
  type StudioEditorialTranscriptSpan,
  type StudioEditorialVisualEvidence,
  type StudioScope,
} from "./studio-capabilities";
import {
  DeterministicStudioRenderer,
  createStudioShareOutput,
  inspectStudioOutputQuality,
  type StudioRenderSource,
} from "./studio-ffmpeg-renderer";
import {
  assessDemandEvidence,
  assessDemandFromEvidence,
  explicitlyRequestedMaterialCards,
  normalizeDemandText,
  visualDemandMismatchReason,
  type AssessDemandEvidenceInput,
} from "./studio-demand-rules";
import {
  mergeStudioEvidence,
  type MergedStudioEvidence,
  type StudioEvidenceDegradedReason,
  type StudioEvidenceRecord,
} from "./studio-evidence-merger";
import {
  StudioPreferenceRanker,
  type RankedStudioPreferenceCandidate,
  type StudioPreferenceCandidate,
} from "./studio-preference-ranker";
import {
  assertCustomerFeedbackConsistency,
  buildOfflineRevisionProposal,
  normalizeOfflineFeedbackPhrase,
} from "./studio-feedback-rules";

export type CreateDemandInput = Readonly<Omit<ContentDemand,
  "id" | "customerId" | "sessionId" | "version" | "status" | "createdAt"> & {
  operationKey: string;
}>;

export type DiscoverOpportunitiesInput = Readonly<{
  demandId: string;
  matchAssessmentId: string;
}>;

export type AnalyzeProviderEvidenceInput = Readonly<{
  assetId: string;
  sourceStartMs: number;
  sourceEndMs: number;
  contentType: MaterialCard["contentType"];
  receipts: readonly ModelAnalysisReceiptV1[];
  humanFacts: readonly string[];
}>;

export type CaptureIngestResult = Readonly<{
  asset: StudioMediaAsset;
  material: MaterialCard | null;
  status: "ready" | "review-required";
}>;

export type OpportunityDiscovery = Readonly<{
  inventory: MaterialInventory;
  opportunities: readonly ContentOpportunity[];
  gaps: GapList["gaps"];
  gapList: GapList | null;
}>;

export type ConfirmBriefInput = Readonly<{
  opportunityId: string;
  matchAssessmentId: string;
  selectedAssetIds: readonly string[];
  confirmedFactIds: readonly string[];
  reviewRequirements: readonly string[];
  operationKey: string;
}>;

export type RecordFeedbackInput = Readonly<{
  draftId: string;
  action: "adopt" | "reject" | "request-changes";
  reason: string | null;
  operationKey: string;
}>;

export type RecordDistributionEventInput = Readonly<{
  draftId: string;
  action: StudioDistributionEvent["action"];
  operationKey: string;
}>;

export type ReviseDraftCaptionsInput = Readonly<{
  captionTexts: readonly Readonly<{ id: string; text: string }>[];
  style: StudioCaptionStyle;
  operationKey: string;
}>;

export type CreateShareOptimizedDraftInput = Readonly<{
  parentDraftId: string;
  operationKey: string;
}>;

export type ImportApprovedCaptionDraftInput = Readonly<{
  briefId: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  subtitleDigest: string;
  approvedAt: string;
  captionCues: readonly StudioCaptionCue[];
  style: StudioCaptionStyle;
  operationKey: string;
}>;

export type RecordCustomerFeedbackInput = Readonly<{
  draftId: string;
  satisfaction: CustomerDraftFeedback["satisfaction"];
  issueCategories: CustomerDraftFeedback["issueCategories"];
  comment: string | null;
  mustKeep: readonly string[];
  mustRemove: readonly string[];
  needsReshoot: readonly string[];
  submittedBy: CustomerDraftFeedback["submittedBy"];
  operationKey: string;
}>;

export type StudioHistory = Readonly<{
  demands: readonly ContentDemand[];
  assessments: readonly MatchAssessment[];
  opportunities: readonly ContentOpportunity[];
  briefs: readonly StudioCreativeBrief[];
  drafts: readonly DraftPackage[];
  customerFeedback: readonly CustomerDraftFeedback[];
  revisionProposals: readonly RevisionProposal[];
  distributionEvents: readonly StudioDistributionEvent[];
}>;

export type StudioDurableStateV1 = Readonly<{
  schemaVersion: 1;
  history: StudioHistory;
  feedback: readonly FeedbackEvent[];
}>;

export const STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES = Object.freeze([
  Object.freeze({
    direction: "chronological-evidence-first",
    roles: Object.freeze(["process-evidence-open", "process-evidence-progress", "finished-product-close"] as const),
    reasons: Object.freeze(["工序证据开场", "过程证据推进", "成品证据收束"] as const),
  }),
  Object.freeze({
    direction: "designer-viewpoint-first",
    roles: Object.freeze(["designer-viewpoint-open", "design-detail-explanation", "process-evidence-return"] as const),
    reasons: Object.freeze(["设计师观点开场", "细节解释设计观点", "回看工序证据"] as const),
  }),
] as const);

export type OfflineStudioFeedbackAdjustment =
  | "increase-opening-shot-share"
  | "increase-ending-shot-share"
  | "tighten-pacing";

export type OfflineStudioFeedbackInterpretation = Readonly<{
  interpreter: "offline-rule-v2";
  adjustments: OfflineStudioFeedbackAdjustment[];
}>;

const OFFLINE_FEEDBACK_INTERPRETER_REQUIREMENT = "离线反馈解释器：offline-rule-v2";
const OFFLINE_FEEDBACK_RULE_PREFIX = "离线剪辑规则：";
const OFFLINE_FEEDBACK_PHRASES: Readonly<Record<string, OfflineStudioFeedbackAdjustment>> = Object.freeze({
  "请让设计师观点更突出": "increase-opening-shot-share",
  "请提高设计师观点开场的相对占比": "increase-opening-shot-share",
  "请让成品结尾更突出": "increase-ending-shot-share",
  "请提高成品结尾的相对占比": "increase-ending-shot-share",
  "节奏太慢": "tighten-pacing",
});
const OFFLINE_FEEDBACK_REJECTION_MARKERS = /(?:不|别|勿|禁止|删除|去掉|移除|撤销|但|同时|以及|并且|并|然后|[，,；;])/u;

/**
 * Narrow, deterministic offline rules for the local MVP. This intentionally
 * does not claim general natural-language understanding.
 */
export function interpretOfflineStudioFeedback(reasonValue: string): OfflineStudioFeedbackInterpretation {
  const reason = normalizeOfflineFeedbackPhrase(reasonValue);
  if (!reason || OFFLINE_FEEDBACK_REJECTION_MARKERS.test(reason)) {
    return { interpreter: "offline-rule-v2", adjustments: [] };
  }
  const adjustment = OFFLINE_FEEDBACK_PHRASES[reason];
  return {
    interpreter: "offline-rule-v2",
    adjustments: adjustment ? [adjustment] : [],
  };
}

type StudioOptions = Readonly<{
  now?: () => string;
  outputRoot?: string;
  editorial?: EditorialDecisionAdapter;
  resolveEditorialTranscriptSpans?: (
    scope: StudioScope,
    cards: readonly MaterialCard[],
  ) => Promise<readonly StudioEditorialTranscriptSpan[]>;
  resolveEditorialVisualEvidence?: (
    scope: StudioScope,
    cards: readonly MaterialCard[],
  ) => Promise<readonly StudioEditorialVisualEvidence[]>;
  resolveEditorialBeatEvidence?: (
    scope: StudioScope,
    cards: readonly MaterialCard[],
    sources: readonly StudioRenderSource[],
  ) => Promise<readonly StudioEditorialBeatEvidence[]>;
  resolveEditorialMusicTrack?: (
    scope: StudioScope,
    brief: StudioCreativeBrief,
  ) => Promise<StudioEditorialMusicTrack | null>;
  preferenceRanker?: StudioPreferenceRanker;
}>;
type StoredOpportunity = Readonly<{
  actorId: string;
  value: ContentOpportunity;
  cards: readonly MaterialCard[];
}>;
type StoredDemandOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  value: ContentDemand;
}>;
type StoredMatch = Readonly<{
  actorId: string;
  value: MatchAssessment;
  inventoryCardIdentity: string;
  gapList: GapList | null;
}>;
type StoredMatchOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  value: MatchAssessment;
}>;
type StoredBriefOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  value: StudioCreativeBrief;
}>;
type StoredDraftOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  values: readonly DraftPackage[];
}>;
type StoredCaptionRevisionOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  value: DraftPackage;
}>;
type StoredExternalEditReturn = Readonly<{
  actorId: string;
  candidateId: string;
  bodyDigest: string;
  value: DraftPackage;
}>;
type StoredFeedbackOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  value: FeedbackEvent;
}>;
type StoredDistributionOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  value: StudioDistributionEvent;
}>;
type StoredCustomerFeedbackOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  value: CustomerDraftFeedback;
}>;
type StoredRevisionProposalOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  value: RevisionProposal;
}>;
type RevisionConfirmationReceipt = Readonly<{
  actorId: string;
  proposalId: string;
  confirmedAt: string;
}>;
type StoredRevisionConfirmationOperation = Readonly<{
  actorId: string;
  bodyDigest: string;
  value: StudioCreativeBrief;
}>;
export class PrivateContentStudio {
  private readonly now: () => string;
  private readonly demands = new Map<string, ContentDemand>();
  private readonly demandActors = new Map<string, string>();
  private readonly demandOperations = new Map<string, StoredDemandOperation>();
  private readonly demandVersions = new Map<string, number>();
  private readonly currentDemands = new Map<string, string>();
  private readonly matches = new Map<string, StoredMatch>();
  private readonly evidenceByCardId = new Map<string, readonly StudioEvidenceRecord[]>();
  private readonly degradedReasonsByCardId = new Map<string, readonly StudioEvidenceDegradedReason[]>();
  private readonly providerIdentitiesByCardId = new Map<string, readonly StudioModelProviderIdentity[]>();
  private readonly matchOperations = new Map<string, StoredMatchOperation>();
  private readonly opportunities = new Map<string, StoredOpportunity>();
  private readonly briefOperations = new Map<string, StoredBriefOperation>();
  private readonly briefVersions = new Map<string, number>();
  private readonly briefs = new Map<string, StudioCreativeBrief>();
  private readonly briefActors = new Map<string, string>();
  private readonly currentBriefs = new Map<string, string>();
  private readonly blueprints = new Map<string, EditBlueprint>();
  private readonly blueprintActors = new Map<string, string>();
  private readonly drafts = new Map<string, DraftPackage>();
  private readonly draftActors = new Map<string, string>();
  private readonly draftOperations = new Map<string, StoredDraftOperation>();
  private readonly verifiedDraftOutputs = new Map<string, Readonly<{
    outputPath: string;
    outputDigest: string;
    size: number;
    mtimeMs: number;
    ctimeMs: number;
  }>>();
  private readonly captionRevisionOperations = new Map<string, StoredCaptionRevisionOperation>();
  private readonly shareOptimizationOperations = new Map<string, StoredCaptionRevisionOperation>();
  private readonly approvedCaptionImportOperations = new Map<string, StoredCaptionRevisionOperation>();
  private readonly externalEditReturns = new Map<string, StoredExternalEditReturn>();
  private readonly externalEditReturnOperations = new Map<string, StoredExternalEditReturn>();
  private readonly feedback = new Map<string, FeedbackEvent>();
  private readonly feedbackOperations = new Map<string, StoredFeedbackOperation>();
  private readonly distributionEvents = new Map<string, StudioDistributionEvent>();
  private readonly distributionOperations = new Map<string, StoredDistributionOperation>();
  private readonly customerFeedback = new Map<string, CustomerDraftFeedback>();
  private readonly customerFeedbackOperations = new Map<string, StoredCustomerFeedbackOperation>();
  private readonly currentCustomerFeedbackByDraft = new Map<string, string>();
  private readonly revisionProposals = new Map<string, RevisionProposal>();
  private readonly revisionProposalActors = new Map<string, string>();
  private readonly revisionProposalOperations = new Map<string, StoredRevisionProposalOperation>();
  private readonly revisionProposalVersions = new Map<string, number>();
  private readonly currentRevisionProposals = new Map<string, string>();
  private readonly revisionConfirmationReceipts = new Map<string, RevisionConfirmationReceipt>();
  private readonly revisionConfirmationOperations = new Map<string, StoredRevisionConfirmationOperation>();
  private readonly scopeQueues = new Map<string, Promise<void>>();
  private readonly outputRoot: string | null;
  private readonly editorial: EditorialDecisionAdapter;
  private readonly resolveEditorialTranscriptSpans: NonNullable<StudioOptions["resolveEditorialTranscriptSpans"]>;
  private readonly resolveEditorialVisualEvidence: NonNullable<StudioOptions["resolveEditorialVisualEvidence"]>;
  private readonly resolveEditorialBeatEvidence: NonNullable<StudioOptions["resolveEditorialBeatEvidence"]>;
  private readonly resolveEditorialMusicTrack: NonNullable<StudioOptions["resolveEditorialMusicTrack"]>;
  private readonly preferenceRanker: StudioPreferenceRanker | null;

  constructor(
    private readonly library: CustomerMaterialLibrary,
    private readonly understanding: MediaUnderstandingAdapter,
    options: StudioOptions = {},
  ) {
    this.now = options.now ?? (() => new Date().toISOString());
    this.outputRoot = options.outputRoot === undefined ? null : resolve(options.outputRoot);
    this.editorial = options.editorial ?? new OfflineEditorialDecisionAdapter();
    this.resolveEditorialTranscriptSpans = options.resolveEditorialTranscriptSpans ?? (async () => []);
    this.resolveEditorialVisualEvidence = options.resolveEditorialVisualEvidence ?? (async () => []);
    this.resolveEditorialBeatEvidence = options.resolveEditorialBeatEvidence ?? (async () => []);
    this.resolveEditorialMusicTrack = options.resolveEditorialMusicTrack ?? (async () => null);
    this.preferenceRanker = options.preferenceRanker ?? null;
  }

  /**
   * The single hardware-to-studio boundary. A phone/device adapter may deliver
   * bytes only when they are bound to this exact customer and studio session.
   * Captures that still need human permission are stored but never analyzed or
   * exposed through the usable material inventory.
   */
  async ingestCapture(
    scope: StudioScope,
    adapter: CaptureEnvelopeAdapter,
    requestId: string,
    fixture: OfflineAnalysisFixture,
  ): Promise<CaptureIngestResult> {
    assertScope(scope);
    assertText(requestId, "capture-request-id-required");
    const envelope = CaptureEnvelopeV1Schema.parse(
      await adapter.capture({ scope, requestId: requestId.trim() }),
    );
    if (envelope.customerId !== scope.customerId || envelope.binding.customerId !== scope.customerId) {
      throw new Error("capture-customer-mismatch");
    }
    if (envelope.sessionId !== scope.sessionId || envelope.binding.sessionId !== scope.sessionId) {
      throw new Error("capture-session-mismatch");
    }
    if (envelope.mediaKind !== "video") throw new Error("capture-media-kind-not-supported");
    const asset = await this.library.importCaptureEnvelope(
      scope.actorId,
      scope.customerId,
      envelope,
    );
    if (asset.viewPermission !== "allowed" || asset.usePermission !== "allowed") {
      return clone({ asset, material: null, status: "review-required" });
    }
    const material = await this.analyze(scope, asset.id, fixture);
    return clone({ asset, material, status: "ready" });
  }

  async analyze(
    scope: StudioScope,
    assetId: string,
    fixture: OfflineAnalysisFixture,
  ): Promise<MaterialCard> {
    assertScope(scope);
    const currentCards = await this.library.inventoryAt(
      scope.actorId,
      scope.customerId,
      this.timestamp(),
      scope.sessionId,
    );
    const asset = (await this.library.listAssets(scope.actorId, scope.customerId))
      .find((candidate) => candidate.id === assetId);
    if (!asset) throw new Error("asset-not-found");
    if (asset.usePermission !== "allowed") throw new Error("asset-use-not-allowed");
    const originalBytes = await this.library.readOriginal(scope.actorId, scope.customerId, assetId);
    const capability = snapshotCapability(this.understanding.capability);
    const adapterCard = await this.understanding.analyze({
      scope,
      asset,
      originalBytes,
      fixture,
      capability,
      createdAt: this.timestamp(),
    });
    const card = validateAdapterCard(
      adapterCard,
      capability,
      scope,
      asset.id,
      asset.digest,
    );
    const existing = currentCards.find((candidate) => candidate.id === card.id);
    if (existing) {
      if (!sameAnalysisBody(existing, card)) throw new Error("immutable-material-card-conflict");
      return clone(existing);
    }
    return this.library.appendMaterialCard(scope.actorId, scope.customerId, card);
  }

  async analyzeProviderEvidence(
    scope: StudioScope,
    input: AnalyzeProviderEvidenceInput,
  ): Promise<MergedStudioEvidence> {
    assertScope(scope);
    assertText(input?.assetId, "asset-id-required");
    if (!Array.isArray(input?.receipts) || !Array.isArray(input?.humanFacts)) {
      throw new Error("studio-evidence-arrays-required");
    }
    let asset: StudioMediaAsset;
    let sourcePath: string;
    try {
      const currentSource = await this.library.inspectAnalysisAsset(
        scope.actorId,
        scope.customerId,
        scope.sessionId,
        input.assetId.trim(),
      );
      asset = currentSource.asset;
      sourcePath = currentSource.sourcePath;
    } catch (error) {
      if (isProviderEvidencePermissionError(error)) throw new Error("studio-evidence-permission-blocked");
      throw error;
    }
    const currentCards = await this.library.inventoryAt(
      scope.actorId, scope.customerId, this.timestamp(), scope.sessionId,
    );
    const version = currentCards
      .filter((card) => card.sourceAssetId === asset.id)
      .reduce((maximum, card) => Math.max(maximum, card.version), 0) + 1;
    const merged = mergeStudioEvidence({
      scope,
      asset,
      sourcePath,
      sourceStartMs: input.sourceStartMs,
      sourceEndMs: input.sourceEndMs,
      contentType: input.contentType,
      receipts: [...input.receipts],
      humanFacts: [...input.humanFacts],
      createdAt: this.timestamp(),
      version,
    });
    const existing = currentCards.find((card) => card.id === merged.card.id);
    const card = existing ?? await this.library.appendMaterialCard(scope.actorId, scope.customerId, merged.card);
    if (existing && !sameProviderAnalysisIdentity(existing, merged.card)) {
      throw new Error("immutable-material-card-conflict");
    }
    this.evidenceByCardId.set(card.id, merged.evidence);
    this.degradedReasonsByCardId.set(card.id, merged.degradedReasons);
    this.providerIdentitiesByCardId.set(card.id, merged.providerIdentities);
    return clone({ ...merged, card });
  }

  async inventory(scope: StudioScope, asOf = this.timestamp()): Promise<MaterialInventory> {
    return (await this.inventoryWithCards(scope, asOf)).inventory;
  }

  async createDemand(scope: StudioScope, input: CreateDemandInput): Promise<ContentDemand> {
    assertScope(scope);
    assertCreateDemandInput(input);
    const normalized = {
      contentType: input.contentType,
      goal: normalizeDemandText(input.goal),
      targetAudience: normalizeDemandText(input.targetAudience),
      desiredEffect: normalizeDemandText(input.desiredEffect),
      mustInclude: normalizeDemandValues(input.mustInclude, "demand-must-include-invalid"),
      mustAvoid: normalizeDemandValues(input.mustAvoid, "demand-must-avoid-invalid"),
      submittedBy: input.submittedBy,
    };
    assertDemandLimits(normalized);
    const operationValue = normalizeOperationKey(input.operationKey, "demand-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    const bodyDigest = stableDigest(normalized);
    return this.withScopeLock(scope, async () => {
      const existing = this.demandOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) {
        throw new Error("demand-operation-actor-mismatch");
      }
      if (existing && existing.bodyDigest !== bodyDigest) {
        throw new Error("demand-operation-key-conflict");
      }
      if (existing) return clone(existing.value);

      const versionKey = this.demandVersionKey(scope);
      const version = (this.demandVersions.get(versionKey) ?? 0) + 1;
      const demand = ContentDemandSchema.parse({
        id: `demand_${stableDigest({ scope: scopedIdentity(scope), operationKey: operationValue })}`,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version,
        ...normalized,
        status: "current",
        createdAt: this.timestamp(),
      });
      this.demands.set(demand.id, demand);
      this.demandActors.set(demand.id, scope.actorId);
      this.demandOperations.set(operationKey, { actorId: scope.actorId, bodyDigest, value: demand });
      this.demandVersions.set(versionKey, version);
      this.currentDemands.set(versionKey, demand.id);
      return clone(demand);
    });
  }

  async getDemand(scope: StudioScope, demandId: string): Promise<ContentDemand> {
    assertScope(scope);
    assertText(demandId, "demand-id-required");
    const demand = this.requireScopedDemand(scope, demandId.trim());
    const status = this.currentDemands.get(this.demandVersionKey(scope)) === demand.id
      ? "current" as const
      : "superseded" as const;
    return clone({ ...demand, status });
  }

  async assessDemand(
    scope: StudioScope,
    demandIdValue: string,
    operationKeyValue: string,
  ): Promise<MatchAssessment> {
    assertScope(scope);
    assertText(demandIdValue, "demand-id-required");
    assertText(operationKeyValue, "match-operation-key-required");
    const demandId = demandIdValue.trim();
    const operationValue = normalizeOperationKey(operationKeyValue, "match-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    const bodyDigest = stableDigest({ demandId });
    return this.withScopeLock(scope, async () => {
      const existing = this.matchOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) {
        throw new Error("match-operation-actor-mismatch");
      }
      if (existing && existing.bodyDigest !== bodyDigest) {
        throw new Error("match-operation-key-conflict");
      }
      const demand = this.requireScopedDemand(scope, demandId);
      this.assertDemandCurrent(scope, demand);
      if (existing) return clone(existing.value);

      const snapshot = await this.inventoryWithCards(scope, this.timestamp());
      const version = [...this.matches.values()]
        .filter((stored) => stored.value.demandId === demand.id).length + 1;
      const assessmentInput: AssessDemandEvidenceInput = {
        identity: {
          id: `match_${stableDigest({ scope: scopedIdentity(scope), demandId, operationKey: operationValue })}`,
          customerId: scope.customerId,
          sessionId: scope.sessionId,
          version,
        },
        demand,
        inventory: snapshot.inventory,
        cards: snapshot.cards,
        capability: snapshot.inventory.capability,
        createdAt: this.timestamp(),
        canRenderTwoDrafts: (cards, requiredEvidence) => {
          const result = searchDemandBoundDraftCards(
            cards,
            requiredEvidence.map((row) => row.materialCardIds),
          );
          return {
            canRender: result.cards !== null,
            budgetExceeded: result.diagnostics.budgetExceeded,
          };
        },
      };
      const providerBacked = snapshot.cards.length > 0 &&
        snapshot.cards.every((card) => this.evidenceByCardId.has(card.id));
      const evidenceResult = providerBacked
        ? assessDemandFromEvidence({
            ...assessmentInput,
            evidenceByCardId: new Map(snapshot.cards.map((card) => [card.id, this.evidenceByCardId.get(card.id)!])),
            degradedReasons: snapshot.cards.flatMap((card) => this.degradedReasonsByCardId.get(card.id) ?? []),
          })
        : { assessment: assessDemandEvidence(assessmentInput), gapList: null };
      const explicitlyRequested = explicitlyRequestedMaterialCards(snapshot.cards, demand);
      const visualEvidence = explicitlyRequested.length === 1
        ? await this.resolveEditorialVisualEvidence(scope, clone(explicitlyRequested))
        : [];
      const visualMismatch = visualDemandMismatchReason(demand.contentType, demand.goal, visualEvidence);
      const assessment = visualMismatch === null
        ? evidenceResult.assessment
        : MatchAssessmentSchema.parse({
            ...evidenceResult.assessment,
            verdict: "blocked",
            unmetNeeds: [...new Set([...evidenceResult.assessment.unmetNeeds, visualMismatch])],
            recommendedCaptures: [...new Set([
              ...evidenceResult.assessment.recommendedCaptures,
              "改为风景内容需求，或重新选择能清楚看到商品主体的历史素材",
            ])],
            safeScope: "视觉模型发现所选画面与商品介绍目标不一致；不会进入任务书或生成成片，人工可改需求或换素材后重试",
          });
      const stored = {
        actorId: scope.actorId,
        value: assessment,
        inventoryCardIdentity: stableDigest(cardIdentity(snapshot.cards)),
        gapList: evidenceResult.gapList,
      };
      this.matches.set(assessment.id, stored);
      this.matchOperations.set(operationKey, { actorId: scope.actorId, bodyDigest, value: assessment });
      return clone(assessment);
    });
  }

  async getMatchAssessment(scope: StudioScope, assessmentId: string): Promise<MatchAssessment> {
    assertScope(scope);
    assertText(assessmentId, "match-assessment-id-required");
    return clone(this.requireScopedMatch(scope, assessmentId.trim()).value);
  }

  async rankMaterials(
    scope: StudioScope,
    demandIdValue: string,
  ): Promise<RankedStudioPreferenceCandidate[]> {
    assertScope(scope);
    assertText(demandIdValue, "demand-id-required");
    const demand = this.requireScopedDemand(scope, demandIdValue.trim());
    this.assertDemandCurrent(scope, demand);
    const snapshot = await this.inventoryWithCards(scope, this.timestamp());
    const assessment = [...this.matches.values()]
      .filter((stored) =>
        stored.actorId === scope.actorId
        && stored.value.customerId === scope.customerId
        && stored.value.sessionId === scope.sessionId
        && stored.value.demandId === demand.id)
      .sort((left, right) => right.value.version - left.value.version)[0];
    if (!assessment) throw new Error("match-assessment-not-found");
    this.assertMatchInventoryCurrent(assessment, snapshot.cards);
    const matchedIds = new Set(
      assessment.value.matchedEvidence.flatMap((row) => row.materialCardIds),
    );
    const candidates: StudioPreferenceCandidate[] = snapshot.cards.map((card) => {
      const durationMs = card.sourceEndMs - card.sourceStartMs;
      const eligible = matchedIds.has(card.id)
        && card.contentType === demand.contentType
        && card.technicalUsable
        && durationMs >= 8_000
        && durationMs <= 20_000;
      return {
        materialCardId: card.id,
        customerId: card.customerId,
        sessionId: card.sessionId,
        sourceAssetId: card.sourceAssetId,
        sourceDigest: card.sourceDigest,
        sourceStartMs: card.sourceStartMs,
        sourceEndMs: card.sourceEndMs,
        contentType: card.contentType,
        eligible,
        permission: "allowed",
        technicalUsable: card.technicalUsable,
        technicalBlockers: [...card.technicalFlags],
        confirmedFacts: [...card.confirmedFacts],
        baseScore: eligible ? 0.6 : 0,
      };
    });
    await this.reconcileDraftPreferences(scope);
    return this.requirePreferenceRanker().rank(scope, candidates, demand);
  }

  async discoverOpportunities(
    scope: StudioScope,
    input: DiscoverOpportunitiesInput,
  ): Promise<OpportunityDiscovery> {
    assertScope(scope);
    assertText(input?.demandId, "demand-id-required");
    assertText(input?.matchAssessmentId, "match-assessment-id-required");
    const demand = this.requireScopedDemand(scope, input.demandId.trim());
    this.assertDemandCurrent(scope, demand);
    const storedMatch = this.requireScopedMatch(scope, input.matchAssessmentId.trim());
    if (storedMatch.value.demandId !== demand.id) throw new Error("match-demand-mismatch");
    const snapshot = await this.inventoryWithCards(scope, this.timestamp());
    this.assertMatchInventoryCurrent(storedMatch, snapshot.cards);
    if (storedMatch.value.verdict !== "ready") {
      if (storedMatch.gapList) {
        return clone({
          inventory: snapshot.inventory,
          opportunities: [],
          gaps: storedMatch.gapList.gaps,
          gapList: storedMatch.gapList,
        });
      }
      throw new Error("match-not-ready");
    }
    const visibleAssets = await this.library.listAssets(scope.actorId, scope.customerId);
    const videoAssetIds = new Set(
      visibleAssets.filter((asset) => asset.mediaKind === "video").map((asset) => asset.id),
    );
    const matchedCardIds = new Set(storedMatch.value.matchedEvidence
      .flatMap((row) => row.materialCardIds));
    const explicitlyRequested = explicitlyRequestedMaterialCards(snapshot.cards, demand);
    const singleSourceRequested = explicitlyRequested.length === 1;
    const eligible = (singleSourceRequested ? explicitlyRequested : snapshot.cards).filter((card) =>
      matchedCardIds.has(card.id) &&
      card.contentType === demand.contentType &&
      card.technicalUsable &&
      videoAssetIds.has(card.sourceAssetId) &&
      (singleSourceRequested ? supportsSingleSourceDraftDirections(card) : isEightToTwentySecondSegment(card)));

    const requiredEvidence = requiredEvidenceCardIdGroups(demand, storedMatch.value);
    const selectedCards = singleSourceRequested && eligible.length === 1
      ? eligible
      : findDemandBoundDraftCards(eligible, requiredEvidence);
    if (!selectedCards) throw new Error("match-not-ready");
    const opportunity = ContentOpportunitySchema.parse({
      id: `opportunity_${stableDigest({
        scope: scopedIdentity(scope),
        demandId: demand.id,
        matchAssessmentId: storedMatch.value.id,
        cards: cardIdentity(selectedCards),
      })}`,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 1,
      demandId: demand.id,
      matchAssessmentId: storedMatch.value.id,
      contentType: demand.contentType,
      goal: demand.goal,
      materialCardIds: selectedCards.map((card) => card.id),
      rationale: singleSourceRequested
        ? "已按客户点名锁定这一条视频；将在同一原片内分段组成两套可审核技术初稿，不混入其他素材"
        : "四段素材均已通过客户授权、来源摘要和 8—20 秒技术可用性检查，可组成两套 30—60 秒且选片不同的草稿",
      capability: storedMatch.value.capability,
      createdAt: this.timestamp(),
    });
    this.opportunities.set(this.opportunityKey(scope, opportunity.id), {
      actorId: scope.actorId,
      value: opportunity,
      cards: selectedCards,
    });
    return clone({
      inventory: snapshot.inventory,
      opportunities: [opportunity],
      gaps: [],
      gapList: null,
    });
  }

  async confirmBrief(scope: StudioScope, input: ConfirmBriefInput): Promise<StudioCreativeBrief> {
    assertScope(scope);
    assertConfirmBriefInput(input);
    const normalized = {
      opportunityId: input.opportunityId.trim(),
      matchAssessmentId: input.matchAssessmentId.trim(),
      selectedAssetIds: normalizeUnique(input.selectedAssetIds, "selected-asset-id-required"),
      confirmedFactIds: normalizeUnique(input.confirmedFactIds, "confirmed-fact-id-required"),
      reviewRequirements: normalizeUnique(input.reviewRequirements, "review-requirement-required"),
    };
    if (normalized.reviewRequirements.length === 0) throw new Error("review-requirements-required");

    const bodyDigest = stableDigest(normalized);
    const operationValue = normalizeOperationKey(input.operationKey, "brief-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    return this.withScopeLock(scope, async () => {
      const stored = this.opportunities.get(this.opportunityKey(scope, normalized.opportunityId));
      if (!stored) throw new Error("opportunity-not-found");
      const existing = this.briefOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) {
        throw new Error("brief-operation-actor-mismatch");
      }
      if (existing && existing.bodyDigest !== bodyDigest) {
        throw new Error("brief-operation-key-conflict");
      }
      if (stored.actorId !== scope.actorId) throw new Error("opportunity-actor-mismatch");
      const singleSourceOpportunity = stored.cards.length === 1 &&
        supportsSingleSourceDraftDirections(stored.cards[0]!);
      if (normalized.selectedAssetIds.length < (singleSourceOpportunity ? 1 : 4)) {
        throw new Error(singleSourceOpportunity ? "single-selected-asset-required" : "four-selected-assets-required");
      }

      if (stored.value.matchAssessmentId !== normalized.matchAssessmentId) {
        throw new Error("match-assessment-mismatch");
      }
      const demand = this.requireScopedDemand(scope, stored.value.demandId);
      this.assertDemandCurrent(scope, demand);
      const storedMatch = this.requireScopedMatch(scope, normalized.matchAssessmentId);
      if (storedMatch.value.demandId !== demand.id) throw new Error("match-demand-mismatch");
      if (storedMatch.value.verdict !== "ready") throw new Error("match-not-ready");
      const currentSnapshot = await this.inventoryWithCards(scope, this.timestamp());
      this.assertMatchInventoryCurrent(storedMatch, currentSnapshot.cards);
      if (existing) return clone(existing.value);

      const supportingByAsset = new Map(stored.cards.map((card) => [card.sourceAssetId, card]));
      const matchedCardIds = new Set(storedMatch.value.matchedEvidence
        .flatMap((row) => row.materialCardIds));
      if (normalized.selectedAssetIds.some((assetId) => {
        const card = supportingByAsset.get(assetId);
        return !card || !matchedCardIds.has(card.id);
      })) {
        throw new Error("selected-asset-not-in-match");
      }
      if (normalized.selectedAssetIds.some((assetId) => !supportingByAsset.has(assetId))) {
        throw new Error("selected-asset-not-in-opportunity");
      }
      const selectedCards = normalized.selectedAssetIds.map((assetId) => supportingByAsset.get(assetId)!);
      const requiredEvidence = requiredEvidenceCardIdGroups(demand, storedMatch.value);
      if (requiredEvidence.some((cardIds) =>
        !selectedCards.some((card) => cardIds.includes(card.id)))) {
        throw new Error("selected-assets-missing-required-evidence");
      }
      if (!(selectedCards.length === 1
        ? supportsSingleSourceDraftDirections(selectedCards[0]!)
        : supportsDefaultDraftDirections(selectedCards))) {
        throw new Error("selected-assets-cannot-form-two-drafts");
      }
      const currentCards = await this.library.inventoryAt(
        scope.actorId,
        scope.customerId,
        this.timestamp(),
        scope.sessionId,
      );
      const currentCardIds = new Set(currentCards.map((card) => card.id));
      if (normalized.selectedAssetIds.some((assetId) => {
        const card = supportingByAsset.get(assetId);
        return !card || !card.technicalUsable || !currentCardIds.has(card.id);
      })) {
        throw new Error("selected-asset-not-eligible");
      }
      const pendingFacts = new Set(
        normalized.selectedAssetIds.flatMap((assetId) => supportingByAsset.get(assetId)?.pendingFacts ?? []),
      );
      if (normalized.confirmedFactIds.some((factId) => !pendingFacts.has(factId))) {
        throw new Error("confirmed-fact-not-pending");
      }
      const versionKey = `${scope.customerId}\0${scope.sessionId}\0${stored.value.id}`;
      const version = (this.briefVersions.get(versionKey) ?? 0) + 1;
      const brief = StudioCreativeBriefSchema.parse({
        id: `brief_${stableDigest({
          kind: "initial-confirmation",
          scope: scopedIdentity(scope),
          opportunityId: stored.value.id,
          operationKey: operationValue,
        })}`,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version,
        demandId: demand.id,
        matchAssessmentId: storedMatch.value.id,
        parentBriefId: null,
        feedbackId: null,
        revisionProposalId: null,
        opportunityId: stored.value.id,
        contentType: stored.value.contentType,
        goal: stored.value.goal,
        selectedAssetIds: normalized.selectedAssetIds,
        confirmedFactIds: normalized.confirmedFactIds,
        reviewRequirements: normalized.reviewRequirements,
        status: "confirmed",
        capability: storedMatch.value.capability,
        createdAt: this.timestamp(),
      });
      this.assertImmutableBriefSlot(brief, scope.actorId);
      this.briefOperations.set(operationKey, { actorId: scope.actorId, bodyDigest, value: brief });
      this.briefVersions.set(versionKey, version);
      this.briefs.set(brief.id, brief);
      this.briefActors.set(brief.id, scope.actorId);
      this.currentBriefs.set(versionKey, brief.id);
      return clone(brief);
    });
  }

  async getBrief(scope: StudioScope, briefId: string): Promise<StudioCreativeBrief> {
    assertScope(scope);
    assertText(briefId, "brief-id-required");
    const brief = this.requireScopedBrief(scope, briefId);
    return clone(brief);
  }

  async getBlueprint(scope: StudioScope, blueprintId: string): Promise<EditBlueprint> {
    assertScope(scope);
    assertText(blueprintId, "blueprint-id-required");
    const blueprint = this.blueprints.get(blueprintId);
    if (!blueprint || blueprint.customerId !== scope.customerId || blueprint.sessionId !== scope.sessionId) {
      throw new Error("blueprint-not-found");
    }
    if (this.blueprintActors.get(blueprintId) !== scope.actorId) throw new Error("blueprint-actor-mismatch");
    return clone(blueprint);
  }

  async createDrafts(
    scope: StudioScope,
    briefId: string,
    operationKeyValue: string,
  ): Promise<DraftPackage[]> {
    assertScope(scope);
    assertText(briefId, "brief-id-required");
    assertText(operationKeyValue, "draft-operation-key-required");
    const outputRoot = this.outputRoot;
    if (!outputRoot) throw new Error("studio-output-root-required");
    const operationValue = normalizeOperationKey(operationKeyValue, "draft-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    const bodyDigest = stableDigest({ briefId: briefId.trim() });

    return this.withScopeLock(scope, async () => {
      const existing = this.draftOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) throw new Error("draft-operation-actor-mismatch");
      if (existing && existing.bodyDigest !== bodyDigest) throw new Error("draft-operation-key-conflict");
      const brief = this.requireScopedBrief(scope, briefId);
      this.assertBriefCurrent(brief);
      try {
        await this.assertBriefDemandMatchCurrent(scope, brief);
      } catch (error) {
        if (error instanceof Error && error.message === "match-inventory-not-current") {
          await this.currentRenderInputs(scope, brief, "draft-source-withdrawn");
        }
        throw error;
      }
      if (existing) {
        await this.assertDraftSourcesCurrent(scope, existing.values, "draft-source-withdrawn");
        return clone(existing.values.map((draft) => this.deriveDraftStatus(draft)));
      }

      const current = await this.currentRenderInputs(scope, brief, "draft-source-withdrawn");
      const capability = snapshotCapability(this.editorial.capability, "editorial-adapter-contract-mismatch");
      const transcriptSpans = await this.resolveEditorialTranscriptSpans(scope, clone(current.cards));
      const visualEvidence = await this.resolveEditorialVisualEvidence(scope, clone(current.cards));
      const beatEvidence = await this.resolveEditorialBeatEvidence(scope, clone(current.cards), clone(current.sources));
      const musicTrack = await this.resolveEditorialMusicTrack(scope, clone(brief));
      const proposed = await this.editorial.createBlueprints({
        scope,
        brief: clone(brief),
        cards: clone(current.cards),
        transcriptSpans: clone(transcriptSpans),
        visualEvidence: clone(visualEvidence),
        beatEvidence: clone(beatEvidence),
        musicTrack: musicTrack === null ? null : {
          assetId: musicTrack.assetId,
          digest: musicTrack.digest,
          fileName: musicTrack.fileName,
          durationSeconds: musicTrack.durationSeconds,
          mixPreference: clone(musicTrack.mixPreference),
          rights: clone(musicTrack.rights),
        },
        createdAt: this.timestamp(),
      });
      const blueprints = validateBlueprints(proposed, capability, scope, brief, current.cards);
      if (!sameCapability(this.editorial.capability, capability)) {
        throw new Error("editorial-adapter-contract-mismatch");
      }

      const rendererSources = musicTrack === null ? current.sources : [
        ...current.sources,
        { assetId: musicTrack.assetId, blobDigest: musicTrack.digest, bytes: musicTrack.bytes },
      ];
      const renderer = new DeterministicStudioRenderer(rendererSources, { outputRoot });
      const createdPaths: string[] = [];
      try {
        const drafts: DraftPackage[] = [];
        for (const blueprint of blueprints) {
          const blueprintDigest = stableDigest(blueprint);
          const outputFileName = `render_${stableDigest({
            briefId: brief.id,
            briefVersion: brief.version,
            blueprintId: blueprint.id,
            blueprintDigest,
          })}.mp4`;
          const outputPath = join(
            outputRoot,
            stableDigest(scopedIdentity(scope)),
            outputFileName,
          );
          await mkdir(dirname(outputPath), { recursive: true });
          const rendered = await renderer.render(blueprint, outputPath);
          createdPaths.push(rendered.outputPath);
          if (rendered.qualityReport.status === "failed") {
            throw new Error("draft-quality-check-failed");
          }
          const draft = DraftPackageSchema.parse({
            id: `draft_${stableDigest({ blueprintId: blueprint.id, blueprintDigest })}`,
            customerId: scope.customerId,
            sessionId: scope.sessionId,
            version: brief.version,
            demandId: brief.demandId,
            revisionProposalId: brief.revisionProposalId,
            briefId: brief.id,
            platformVariant: blueprint.platformVariant,
            blueprintId: blueprint.id,
            blueprintDigest,
            outputDigest: rendered.outputDigest,
            outputPath: rendered.outputPath,
            durationSeconds: rendered.durationSeconds,
            sourceReuseMode: blueprint.sourceReuseMode,
            sourceMap: blueprint.shots.map((shot) => ({
              shotId: shot.id,
              sourceAssetId: shot.sourceAssetId,
              sourceDigest: shot.sourceDigest,
              sourceStartMs: shot.sourceStartMs,
              sourceEndMs: shot.sourceEndMs,
              reason: shot.reason,
            })),
            captionCues: blueprint.captionCues,
            captionStyle: blueprint.captionStyle,
            transitionPlan: blueprint.transitionPlan,
            beatSyncPlan: blueprint.beatSyncPlan,
            musicMixPlan: blueprint.musicMixPlan,
            qualityReport: rendered.qualityReport,
            visualEvidence: buildDraftVisualEvidence(blueprint, visualEvidence),
            status: "reviewable",
            capability,
            publishClaimed: false,
            createdAt: this.timestamp(),
          });
          drafts.push(draft);
        }
        await this.assertDraftSourcesCurrent(scope, drafts, "draft-source-withdrawn");
        for (const blueprint of blueprints) {
          this.blueprints.set(blueprint.id, blueprint);
          this.blueprintActors.set(blueprint.id, scope.actorId);
        }
        for (const draft of drafts) {
          this.drafts.set(draft.id, draft);
          this.draftActors.set(draft.id, scope.actorId);
        }
        this.draftOperations.set(operationKey, { actorId: scope.actorId, bodyDigest, values: drafts });
        return clone(drafts);
      } catch (error) {
        await Promise.all(createdPaths.map((path) => rm(path, { force: true })));
        throw error;
      }
    });
  }

  /**
   * Installs an already human-approved subtitle chapter as a new local draft.
   * The source bytes stay immutable; the renderer writes a separate reviewable
   * MP4 and records the exact source/subtitle digests used for the import.
   */
  async importApprovedCaptionDraft(
    scope: StudioScope,
    input: ImportApprovedCaptionDraftInput,
  ): Promise<DraftPackage> {
    assertScope(scope);
    assertText(input.briefId, "brief-id-required");
    assertText(input.sourceAssetId, "source-asset-id-required");
    assertText(input.sourceDigest, "source-digest-required");
    assertText(input.subtitleDigest, "subtitle-digest-required");
    assertText(input.operationKey, "approved-caption-operation-key-required");
    const outputRoot = this.outputRoot;
    if (!outputRoot) throw new Error("studio-output-root-required");
    if (!/^[a-f0-9]{64}$/u.test(input.sourceDigest) || !/^[a-f0-9]{64}$/u.test(input.subtitleDigest)) {
      throw new Error("approved-caption-digest-invalid");
    }
    if (!Number.isInteger(input.sourceStartMs) || !Number.isInteger(input.sourceEndMs)
        || input.sourceStartMs < 0 || input.sourceEndMs <= input.sourceStartMs
        || input.sourceEndMs - input.sourceStartMs < 6_000
        || input.sourceEndMs - input.sourceStartMs > 60_000) {
      throw new Error("approved-caption-source-range-invalid");
    }
    if (!Number.isFinite(Date.parse(input.approvedAt))) throw new Error("approved-caption-date-invalid");
    const operationValue = normalizeOperationKey(input.operationKey, "approved-caption-operation-key-too-long");
    const style = StudioCaptionStyleSchema.parse(input.style);
    const captionCues = input.captionCues.map((cue) => StudioCaptionCueSchema.parse(cue));
    if (captionCues.length === 0 || captionCues.some((cue) => cue.reviewStatus !== "human-approved")) {
      throw new Error("approved-caption-human-review-required");
    }
    if (new Set(captionCues.map((cue) => cue.id)).size !== captionCues.length) {
      throw new Error("duplicate-approved-caption-cue-id");
    }
    const normalized = {
      briefId: input.briefId.trim(),
      sourceAssetId: input.sourceAssetId.trim(),
      sourceDigest: input.sourceDigest,
      sourceStartMs: input.sourceStartMs,
      sourceEndMs: input.sourceEndMs,
      subtitleDigest: input.subtitleDigest,
      approvedAt: new Date(input.approvedAt).toISOString(),
      captionCues,
      style,
    };
    const bodyDigest = stableDigest(normalized);
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;

    return this.withScopeLock(scope, async () => {
      const priorOperation = this.approvedCaptionImportOperations.get(operationKey);
      if (priorOperation && priorOperation.actorId !== scope.actorId) {
        throw new Error("approved-caption-operation-actor-mismatch");
      }
      if (priorOperation && priorOperation.bodyDigest !== bodyDigest) {
        throw new Error("approved-caption-operation-key-conflict");
      }
      if (priorOperation) return clone(priorOperation.value);

      const brief = this.requireScopedBrief(scope, normalized.briefId);
      this.assertBriefCurrent(brief);
      if (!brief.selectedAssetIds.includes(normalized.sourceAssetId)) {
        throw new Error("approved-caption-source-not-selected");
      }
      const current = await this.currentRenderInputs(scope, brief, "draft-source-withdrawn");
      const card = current.cards.find((candidate) => candidate.sourceAssetId === normalized.sourceAssetId);
      const source = current.sources.find((candidate) => candidate.assetId === normalized.sourceAssetId);
      if (!card || !source || card.sourceDigest !== normalized.sourceDigest
          || source.blobDigest !== normalized.sourceDigest) {
        throw new Error("approved-caption-source-digest-mismatch");
      }
      if (normalized.sourceStartMs < card.sourceStartMs || normalized.sourceEndMs > card.sourceEndMs) {
        throw new Error("approved-caption-source-range-invalid");
      }
      for (const cue of normalized.captionCues) {
        if (cue.sourceAssetId !== normalized.sourceAssetId || cue.sourceDigest !== normalized.sourceDigest
            || cue.sourceStartMs < normalized.sourceStartMs || cue.sourceEndMs > normalized.sourceEndMs
            || cue.timelineStartMs !== cue.sourceStartMs - normalized.sourceStartMs
            || cue.timelineEndMs !== cue.sourceEndMs - normalized.sourceStartMs) {
          throw new Error("approved-caption-cue-binding-mismatch");
        }
      }

      const createdAt = this.timestamp();
      const shot = {
        id: `shot_approved_caption_${stableDigest({
          sourceAssetId: normalized.sourceAssetId,
          sourceStartMs: normalized.sourceStartMs,
          sourceEndMs: normalized.sourceEndMs,
        })}`,
        sourceAssetId: normalized.sourceAssetId,
        sourceDigest: normalized.sourceDigest,
        sourceStartMs: normalized.sourceStartMs,
        sourceEndMs: normalized.sourceEndMs,
        timelineStartMs: 0,
        reason: "人工验收字幕章节导入",
      };
      const blueprint = EditBlueprintSchema.parse({
        id: `blueprint_approved_caption_${stableDigest({ bodyDigest, operationValue })}`,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version: brief.version,
        briefId: brief.id,
        platformVariant: "editorial-master",
        direction: "approved-caption-chapter",
        durationSeconds: (normalized.sourceEndMs - normalized.sourceStartMs) / 1_000,
        shots: [shot],
        captionCues: normalized.captionCues,
        captionStyle: normalized.style,
        capability: brief.capability,
        publishClaimed: false,
        createdAt,
      });
      const blueprintDigest = stableDigest(blueprint);
      const draftId = `draft_approved_caption_${stableDigest({ blueprintId: blueprint.id, blueprintDigest })}`;
      const existingDraft = this.drafts.get(draftId);
      if (existingDraft) {
        if (this.draftActors.get(draftId) !== scope.actorId) throw new Error("draft-actor-mismatch");
        this.approvedCaptionImportOperations.set(operationKey, {
          actorId: scope.actorId,
          bodyDigest,
          value: existingDraft,
        });
        return clone(this.deriveDraftStatus(existingDraft));
      }
      const outputPath = join(
        outputRoot,
        stableDigest(scopedIdentity(scope)),
        `render_approved_caption_${stableDigest({ blueprintId: blueprint.id, blueprintDigest })}.mp4`,
      );
      await mkdir(dirname(outputPath), { recursive: true });
      const renderer = new DeterministicStudioRenderer(current.sources, { outputRoot });
      const rendered = await renderer.render(blueprint, outputPath);
      if (rendered.qualityReport.status === "failed") {
        await rm(rendered.outputPath, { force: true });
        throw new Error("approved-caption-quality-check-failed");
      }
      const draft = DraftPackageSchema.parse({
        id: draftId,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version: brief.version,
        demandId: brief.demandId,
        revisionProposalId: brief.revisionProposalId,
        briefId: brief.id,
        platformVariant: "editorial-master",
        blueprintId: blueprint.id,
        blueprintDigest,
        outputDigest: rendered.outputDigest,
        outputPath: rendered.outputPath,
        durationSeconds: rendered.durationSeconds,
        sourceMap: [{
          shotId: shot.id,
          sourceAssetId: shot.sourceAssetId,
          sourceDigest: shot.sourceDigest,
          sourceStartMs: shot.sourceStartMs,
          sourceEndMs: shot.sourceEndMs,
          reason: shot.reason,
        }],
        captionCues: normalized.captionCues,
        captionStyle: normalized.style,
        qualityReport: rendered.qualityReport,
        approvedCaptionImport: {
          sourceDigest: normalized.sourceDigest,
          subtitleDigest: normalized.subtitleDigest,
          sourceStartMs: normalized.sourceStartMs,
          sourceEndMs: normalized.sourceEndMs,
          approvedAt: normalized.approvedAt,
          approvalScope: "local-human-approved-candidate",
          productionApproved: false,
        },
        status: "reviewable",
        capability: brief.capability,
        publishClaimed: false,
        createdAt,
      });
      await this.assertDraftSourcesCurrent(scope, [draft], "draft-source-withdrawn");
      this.blueprints.set(blueprint.id, blueprint);
      this.blueprintActors.set(blueprint.id, scope.actorId);
      this.drafts.set(draft.id, draft);
      this.draftActors.set(draft.id, scope.actorId);
      this.approvedCaptionImportOperations.set(operationKey, {
        actorId: scope.actorId,
        bodyDigest,
        value: draft,
      });
      return clone(draft);
    });
  }

  async getDraft(scope: StudioScope, draftId: string): Promise<DraftPackage> {
    assertScope(scope);
    const draft = this.requireScopedDraft(scope, draftId);
    if (!await this.areDraftSourcesCurrent(scope, [draft]) || !await this.isDraftOutputCurrent(draft)) {
      return clone({ ...draft, status: "invalidated" as const });
    }
    return clone(this.deriveDraftStatus(draft));
  }

  async reviseDraftCaptions(
    scope: StudioScope,
    draftIdValue: string,
    input: ReviseDraftCaptionsInput,
  ): Promise<DraftPackage> {
    assertScope(scope);
    assertText(draftIdValue, "draft-id-required");
    const outputRoot = this.outputRoot;
    if (!outputRoot) throw new Error("studio-output-root-required");
    const draftId = draftIdValue.trim();
    const operationValue = normalizeOperationKey(input.operationKey, "caption-revision-operation-key-too-long");
    const style = StudioCaptionStyleSchema.parse(input.style);
    const captionTexts = input.captionTexts.map((entry) => ({
      id: String(entry.id ?? "").trim(),
      text: String(entry.text ?? "").trim(),
    }));
    if (captionTexts.length === 0) throw new Error("draft-caption-cues-required");
    if (captionTexts.some((entry) => !entry.id || !entry.text || entry.text.length > 500)) {
      throw new Error("caption-revision-text-invalid");
    }
    if (new Set(captionTexts.map((entry) => entry.id)).size !== captionTexts.length) {
      throw new Error("duplicate-caption-revision-cue-id");
    }
    const bodyDigest = stableDigest({ draftId, captionTexts, style });
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    return this.withScopeLock(scope, async () => {
      const existing = this.captionRevisionOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) throw new Error("caption-revision-operation-actor-mismatch");
      if (existing && existing.bodyDigest !== bodyDigest) throw new Error("caption-revision-operation-key-conflict");
      if (existing) return clone(existing.value);

      const parent = this.requireScopedDraft(scope, draftId);
      if (parent.status === "invalidated") throw new Error("draft-not-reviewable");
      if (!parent.captionCues?.length || !parent.captionStyle) throw new Error("draft-caption-cues-required");
      const textById = new Map(captionTexts.map((entry) => [entry.id, entry.text]));
      if (textById.size !== parent.captionCues.length || parent.captionCues.some((cue) => !textById.has(cue.id))) {
        throw new Error("caption-revision-cue-binding-mismatch");
      }
      await this.assertDraftSourcesCurrent(scope, [parent], "draft-source-withdrawn");
      await this.assertDraftOutputCurrent(parent);
      const brief = this.requireScopedBrief(scope, parent.briefId);
      const current = await this.currentRenderInputs(scope, brief, "draft-source-withdrawn");
      const revisedCues = parent.captionCues.map((cue) => {
        const text = textById.get(cue.id)!;
        return {
          ...cue,
          text,
          reviewStatus: cue.reviewStatus === "human-approved" && text !== cue.text
            ? "model-suggestion" as const
            : cue.reviewStatus,
        };
      });
      const changedCueIds = revisedCues
        .filter((cue, index) => cue.text !== parent.captionCues![index]!.text)
        .map((cue) => cue.id);
      const styleChanged = stableDigest(style) !== stableDigest(parent.captionStyle);
      if (changedCueIds.length === 0 && !styleChanged) throw new Error("caption-revision-no-change");

      let timelineStartMs = 0;
      const shots = parent.sourceMap.map((source) => {
        const shot = {
          id: source.shotId,
          sourceAssetId: source.sourceAssetId,
          sourceDigest: source.sourceDigest,
          sourceStartMs: source.sourceStartMs,
          sourceEndMs: source.sourceEndMs,
          timelineStartMs,
          reason: source.reason,
        };
        timelineStartMs += source.sourceEndMs - source.sourceStartMs;
        return shot;
      });
      const createdAt = this.timestamp();
      const blueprintSeed = { parentDraftId: parent.id, captionTexts, style };
      const blueprint = EditBlueprintSchema.parse({
        id: `blueprint_caption_${stableDigest(blueprintSeed)}`,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version: parent.version,
        briefId: parent.briefId,
        platformVariant: parent.platformVariant,
        direction: "caption-revision",
        durationSeconds: timelineStartMs / 1_000,
        shots,
        captionCues: revisedCues,
        captionStyle: style,
        transitionPlan: parent.transitionPlan,
        beatSyncPlan: parent.beatSyncPlan,
        sourceReuseMode: parent.sourceReuseMode,
        capability: parent.capability,
        publishClaimed: false,
        createdAt,
      });
      const blueprintDigest = stableDigest(blueprint);
      const outputPath = join(
        outputRoot,
        stableDigest(scopedIdentity(scope)),
        `render_caption_${stableDigest({ blueprintId: blueprint.id, blueprintDigest })}.mp4`,
      );
      await mkdir(dirname(outputPath), { recursive: true });
      const renderer = new DeterministicStudioRenderer(current.sources, { outputRoot });
      const rendered = await renderer.render(blueprint, outputPath);
      if (rendered.qualityReport.status === "failed") {
        await rm(rendered.outputPath, { force: true });
        throw new Error("caption-revision-quality-check-failed");
      }
      const draft = DraftPackageSchema.parse({
        ...parent,
        id: `draft_caption_${stableDigest({ parentDraftId: parent.id, blueprintDigest })}`,
        blueprintId: blueprint.id,
        blueprintDigest,
        outputDigest: rendered.outputDigest,
        outputPath: rendered.outputPath,
        durationSeconds: rendered.durationSeconds,
        captionCues: revisedCues,
        captionStyle: style,
        qualityReport: rendered.qualityReport,
        captionRevision: { parentDraftId: parent.id, changedCueIds, styleChanged },
        approvedCaptionImport: changedCueIds.length === 0 ? parent.approvedCaptionImport : undefined,
        status: "reviewable",
        externalEdit: undefined,
        createdAt,
      });
      this.blueprints.set(blueprint.id, blueprint);
      this.blueprintActors.set(blueprint.id, scope.actorId);
      this.drafts.set(draft.id, draft);
      this.draftActors.set(draft.id, scope.actorId);
      this.captionRevisionOperations.set(operationKey, { actorId: scope.actorId, bodyDigest, value: draft });
      return clone(draft);
    });
  }

  async createShareOptimizedDraft(
    scope: StudioScope,
    input: CreateShareOptimizedDraftInput,
  ): Promise<DraftPackage> {
    assertScope(scope);
    assertText(input.parentDraftId, "draft-id-required");
    assertText(input.operationKey, "share-optimization-operation-key-required");
    const outputRoot = this.outputRoot;
    if (!outputRoot) throw new Error("studio-output-root-required");
    const parentDraftId = input.parentDraftId.trim();
    const operationValue = normalizeOperationKey(
      input.operationKey, "share-optimization-operation-key-too-long",
    );
    const normalized = { parentDraftId };
    const bodyDigest = stableDigest(normalized);
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;

    return this.withScopeLock(scope, async () => {
      const existingOperation = this.shareOptimizationOperations.get(operationKey);
      if (existingOperation && existingOperation.actorId !== scope.actorId) {
        throw new Error("share-optimization-operation-actor-mismatch");
      }
      if (existingOperation && existingOperation.bodyDigest !== bodyDigest) {
        throw new Error("share-optimization-operation-key-conflict");
      }
      if (existingOperation) return clone(this.deriveDraftStatus(existingOperation.value));

      const parent = this.requireScopedDraft(scope, parentDraftId);
      if (this.deriveDraftStatus(parent).status !== "adopted") {
        throw new Error("share-optimization-parent-not-adopted");
      }
      await this.assertDraftSourcesCurrent(scope, [parent], "draft-source-withdrawn");
      await this.assertDraftOutputCurrent(parent);
      const seed = {
        parentDraftId: parent.id,
        parentOutputDigest: parent.outputDigest,
        platformVariant: parent.platformVariant ?? null,
        targetWidth: 720,
        targetHeight: 1280,
        mode: "fit-pad-no-crop",
      } as const;
      const draftId = `draft_share_${stableDigest(seed)}`;
      const existingDraft = this.drafts.get(draftId);
      if (existingDraft) {
        if (this.draftActors.get(draftId) !== scope.actorId) throw new Error("draft-actor-mismatch");
        await this.assertDraftOutputCurrent(existingDraft);
        this.shareOptimizationOperations.set(operationKey, {
          actorId: scope.actorId, bodyDigest, value: existingDraft,
        });
        return clone(this.deriveDraftStatus(existingDraft));
      }

      const outputPath = join(
        outputRoot,
        stableDigest(scopedIdentity(scope)),
        `render_share_${stableDigest(seed)}.mp4`,
      );
      await mkdir(dirname(outputPath), { recursive: true });
      let rendered: Awaited<ReturnType<typeof createStudioShareOutput>>;
      try {
        rendered = await createStudioShareOutput(parent.outputPath, outputPath, {
          outputRoot,
          captionCues: parent.captionCues,
          captionStyle: parent.captionStyle,
        });
      } catch (error) {
        await rm(outputPath, { force: true });
        throw error;
      }
      const draft = DraftPackageSchema.parse({
        ...parent,
        id: draftId,
        outputDigest: rendered.outputDigest,
        outputPath: rendered.outputPath,
        durationSeconds: rendered.durationSeconds,
        qualityReport: rendered.qualityReport,
        shareOptimization: {
          parentDraftId: parent.id,
          targetWidth: 720,
          targetHeight: 1280,
          mode: "fit-pad-no-crop",
        },
        status: "reviewable",
        createdAt: this.timestamp(),
      });
      this.drafts.set(draft.id, draft);
      this.draftActors.set(draft.id, scope.actorId);
      this.shareOptimizationOperations.set(operationKey, {
        actorId: scope.actorId, bodyDigest, value: draft,
      });
      return clone(draft);
    });
  }

  async registerExternalEditReturnDraft(
    scope: StudioScope,
    input: Readonly<{
      handoff: ExternalEditHandoff;
      candidate: ExternalEditReturnCandidate;
      operationKey: string;
      copyToOutput: (destinationPath: string) => Promise<void>;
    }>,
  ): Promise<DraftPackage> {
    assertScope(scope);
    const handoff = ExternalEditHandoffSchema.parse(input.handoff);
    const candidate = ExternalEditReturnCandidateSchema.parse(input.candidate);
    const operationKeyValue = normalizeOperationKey(input.operationKey, "external-edit-return-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationKeyValue}`;
    const bodyDigest = stableDigest({ handoffId: handoff.id, candidateId: candidate.id, candidateDigest: candidate.digest });
    return this.withScopeLock(scope, async () => {
      const operation = this.externalEditReturnOperations.get(operationKey);
      if (operation) {
        if (operation.actorId !== scope.actorId) throw new Error("external-edit-return-actor-mismatch");
        if (operation.bodyDigest !== bodyDigest) throw new Error("external-edit-return-operation-key-reused");
        return clone(operation.value);
      }
      const priorReturn = this.externalEditReturns.get(handoff.id);
      if (priorReturn) {
        if (priorReturn.candidateId !== candidate.id) throw new Error("external-edit-already-returned");
        if (priorReturn.actorId !== scope.actorId) throw new Error("external-edit-return-actor-mismatch");
        this.externalEditReturnOperations.set(operationKey, priorReturn);
        return clone(priorReturn.value);
      }
      const parent = this.requireScopedDraft(scope, handoff.parentDraftId);
      if (this.draftActors.get(parent.id) !== scope.actorId) throw new Error("draft-actor-mismatch");
      if (parent.status !== "reviewable"
        || handoff.customerId !== scope.customerId
        || handoff.sessionId !== scope.sessionId
        || handoff.parentDraftVersion !== parent.version
        || handoff.parentDraftDigest !== parent.outputDigest
        || candidate.handoffId !== handoff.id
        || candidate.parentDraftId !== parent.id
        || candidate.status !== "pending-review") {
        throw new Error("external-edit-return-binding-mismatch");
      }
      const outputRoot = this.outputRoot;
      if (!outputRoot) throw new Error("studio-output-root-required");
      const scopeRoot = join(outputRoot, stableDigest(scopedIdentity(scope)));
      await mkdir(scopeRoot, { recursive: true, mode: 0o700 });
      const draftId = `draft_external_${stableDigest({ handoffId: handoff.id, candidateDigest: candidate.digest }).slice(0, 32)}`;
      const outputPath = join(scopeRoot, `${draftId}.mp4`);
      await input.copyToOutput(outputPath);
      const outputMetadata = await lstat(outputPath).catch(() => null);
      if (!outputMetadata?.isFile() || outputMetadata.isSymbolicLink() || outputMetadata.nlink !== 1) {
        throw new Error("external-edit-return-output-invalid");
      }
      const [realOutputRoot, realOutputPath] = await Promise.all([realpath(outputRoot), realpath(outputPath)]);
      if (!isContainedPath(realOutputRoot, realOutputPath)) throw new Error("studio-render-output-outside-root");
      const outputHash = createHash("sha256");
      for await (const chunk of createReadStream(realOutputPath)) outputHash.update(chunk as Buffer);
      const finalMetadata = await stat(realOutputPath);
      if (finalMetadata.dev !== outputMetadata.dev
        || finalMetadata.ino !== outputMetadata.ino
        || finalMetadata.size !== candidate.sizeBytes
        || outputHash.digest("hex") !== candidate.digest) {
        throw new Error("external-edit-return-output-digest-mismatch");
      }
      const qualityReport = await inspectStudioOutputQuality(outputPath, {
        durationSeconds: candidate.durationSeconds,
      }).catch(async (error: unknown) => {
        await rm(outputPath, { force: true });
        throw error;
      });
      const draft = DraftPackageSchema.parse({
        id: draftId,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version: parent.version + 1,
        demandId: parent.demandId,
        revisionProposalId: null,
        briefId: parent.briefId,
        blueprintId: parent.blueprintId,
        blueprintDigest: parent.blueprintDigest,
        outputDigest: candidate.digest,
        outputPath,
        durationSeconds: candidate.durationSeconds,
        qualityReport,
        visualEvidence: parent.visualEvidence,
        sourceMap: parent.sourceMap,
        status: "reviewable",
        capability: parent.capability,
        externalEdit: {
          origin: "external-editor-return",
          externalEditor: "unverified-external-file",
          declaredEditor: "jianying-desktop",
          editorProvenance: "return-folder-only",
          parentDraftId: parent.id,
          handoffId: handoff.id,
          lineageConfidence: "parent-only",
        },
        publishClaimed: false,
        createdAt: this.timestamp(),
      });
      const stored = { actorId: scope.actorId, candidateId: candidate.id, bodyDigest, value: draft };
      this.drafts.set(draft.id, draft);
      this.draftActors.set(draft.id, scope.actorId);
      this.externalEditReturns.set(handoff.id, stored);
      this.externalEditReturnOperations.set(operationKey, stored);
      return clone(draft);
    });
  }

  async readDraftOutput(scope: StudioScope, draftId: string): Promise<Buffer> {
    assertScope(scope);
    const draft = this.requireScopedDraft(scope, draftId);
    await this.assertDraftSourcesCurrent(scope, [draft], "draft-source-withdrawn");
    return this.readVerifiedDraftOutput(draft);
  }

  async recordFeedback(scope: StudioScope, input: RecordFeedbackInput): Promise<FeedbackEvent> {
    assertScope(scope);
    assertFeedbackInput(input);
    const normalized = {
      draftId: input.draftId.trim(),
      action: input.action,
      reason: input.reason === null ? null : input.reason.trim(),
    };
    const operationValue = normalizeOperationKey(input.operationKey, "feedback-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    const bodyDigest = stableDigest(normalized);
    return this.withScopeLock(scope, async () => {
      const existing = this.feedbackOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) throw new Error("feedback-operation-actor-mismatch");
      if (existing && existing.bodyDigest !== bodyDigest) throw new Error("feedback-operation-key-conflict");
      if (existing) {
        if (existing.value.action === "adopt") {
          const draft = this.requireScopedDraft(scope, existing.value.draftId);
          if (draft.qualityReport?.status === "failed") throw new Error("draft-quality-check-failed");
          this.assertSatisfiedCustomerFeedback(draft);
          await this.assertDraftSourcesCurrent(scope, [draft], "draft-source-withdrawn");
          await this.assertDraftOutputCurrent(draft);
          const brief = this.requireScopedBrief(scope, draft.briefId);
          try {
            this.assertBriefCurrent(brief);
            await this.assertBriefDemandMatchCurrent(scope, brief);
          } catch {
            throw new Error("draft-brief-not-current");
          }
        }
        await this.recordDraftPreference(scope, existing.value);
        return clone(existing.value);
      }

      const draft = this.requireScopedDraft(scope, normalized.draftId);
      if (normalized.action === "adopt") {
        if (draft.qualityReport?.status === "failed") throw new Error("draft-quality-check-failed");
        this.assertSatisfiedCustomerFeedback(draft);
        await this.assertDraftSourcesCurrent(scope, [draft], "draft-source-withdrawn");
        await this.assertDraftOutputCurrent(draft);
        const brief = this.requireScopedBrief(scope, draft.briefId);
        try {
          this.assertBriefCurrent(brief);
          await this.assertBriefDemandMatchCurrent(scope, brief);
        } catch {
          throw new Error("draft-brief-not-current");
        }
        const adoptedSibling = [...this.feedback.values()].find((event) => {
          if (event.action !== "adopt") return false;
          const adoptedDraft = this.drafts.get(event.draftId);
          if (!adoptedDraft || adoptedDraft.id === draft.id) return false;
          if (draft.shareOptimization) {
            return adoptedDraft.shareOptimization?.parentDraftId === draft.shareOptimization.parentDraftId
              && adoptedDraft.platformVariant === draft.platformVariant;
          }
          return !adoptedDraft.shareOptimization
            && adoptedDraft.briefId === draft.briefId
            && adoptedDraft.version === draft.version;
        });
        if (adoptedSibling) throw new Error("draft-brief-already-adopted");
      }
      const version = [...this.feedback.values()].filter((event) => event.draftId === draft.id).length + 1;
      const event = FeedbackEventSchema.parse({
        id: `feedback_${stableDigest({ scope: scopedIdentity(scope), operationKey: operationValue })}`,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version,
        draftId: draft.id,
        action: normalized.action,
        reason: normalized.reason,
        actorId: scope.actorId,
        publishClaimed: false,
        createdAt: this.timestamp(),
      });
      await this.commitDraftFeedback(scope, event);
      this.feedback.set(event.id, event);
      this.feedbackOperations.set(operationKey, { actorId: scope.actorId, bodyDigest, value: event });
      await this.preferenceRanker?.reconcilePersistedDraftFeedback(scope);
      return clone(event);
    });
  }

  async listFeedback(scope: StudioScope, draftId: string): Promise<FeedbackEvent[]> {
    assertScope(scope);
    const draft = this.requireScopedDraft(scope, draftId);
    return clone([...this.feedback.values()]
      .filter((event) => event.draftId === draft.id)
      .sort((left, right) => left.version - right.version));
  }

  async recordDistributionEvent(
    scope: StudioScope,
    input: RecordDistributionEventInput,
  ): Promise<StudioDistributionEvent> {
    assertScope(scope);
    assertDistributionEventInput(input);
    const normalized = { draftId: input.draftId.trim(), action: input.action };
    const operationValue = normalizeOperationKey(input.operationKey, "distribution-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    const bodyDigest = stableDigest(normalized);
    return this.withScopeLock(scope, async () => {
      const existing = this.distributionOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) throw new Error("distribution-operation-actor-mismatch");
      if (existing && existing.bodyDigest !== bodyDigest) throw new Error("distribution-operation-key-conflict");
      if (existing) return clone(existing.value);

      const draft = this.requireScopedDraft(scope, normalized.draftId);
      if (this.deriveDraftStatus(draft).status !== "adopted") throw new Error("distribution-draft-not-adopted");
      await this.assertDraftSourcesCurrent(scope, [draft], "draft-source-withdrawn");
      await this.assertDraftOutputCurrent(draft);
      const version = [...this.distributionEvents.values()]
        .filter((event) => event.draftId === draft.id).length + 1;
      const event = StudioDistributionEventSchema.parse({
        id: `distribution_${stableDigest({ scope: scopedIdentity(scope), operationKey: operationValue })}`,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version,
        draftId: draft.id,
        action: normalized.action,
        platformVariant: draft.platformVariant ?? null,
        actorId: scope.actorId,
        publishClaimed: false,
        externalDeliveryClaimed: false,
        createdAt: this.timestamp(),
      });
      this.distributionEvents.set(event.id, event);
      this.distributionOperations.set(operationKey, { actorId: scope.actorId, bodyDigest, value: event });
      return clone(event);
    });
  }

  async recordCustomerFeedback(
    scope: StudioScope,
    input: RecordCustomerFeedbackInput,
  ): Promise<CustomerDraftFeedback> {
    assertScope(scope);
    assertRecordCustomerFeedbackInput(input);
    const normalized = {
      draftId: input.draftId.trim(),
      satisfaction: input.satisfaction,
      issueCategories: normalizeUnique(input.issueCategories, "customer-feedback-issue-category-invalid"),
      comment: input.comment === null ? null : normalizeDemandText(input.comment) || null,
      mustKeep: normalizeStructuredValues(input.mustKeep, "customer-feedback-must-keep-invalid"),
      mustRemove: normalizeStructuredValues(input.mustRemove, "customer-feedback-must-remove-invalid"),
      needsReshoot: normalizeStructuredValues(input.needsReshoot, "customer-feedback-reshoot-invalid"),
      submittedBy: input.submittedBy,
    };
    assertCustomerFeedbackLimits(normalized);
    if (normalized.satisfaction !== "satisfied" &&
        normalized.issueCategories.length === 0 && normalized.comment === null &&
        normalized.mustKeep.length === 0 && normalized.mustRemove.length === 0 &&
        normalized.needsReshoot.length === 0) {
      throw new Error("customer-feedback-detail-required");
    }
    assertCustomerFeedbackConsistency(input);
    const operationValue = normalizeOperationKey(input.operationKey, "customer-feedback-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    const bodyDigest = stableDigest(normalized);
    return this.withScopeLock(scope, async () => {
      const existing = this.customerFeedbackOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) {
        throw new Error("customer-feedback-operation-actor-mismatch");
      }
      if (existing && existing.bodyDigest !== bodyDigest) {
        throw new Error("customer-feedback-operation-key-conflict");
      }
      const draft = this.requireScopedDraft(scope, normalized.draftId);
      await this.assertCustomerFeedbackDraftCurrent(scope, draft);
      if (existing) return clone(existing.value);

      const brief = this.requireScopedBrief(scope, draft.briefId);
      const version = [...this.customerFeedback.values()]
        .filter((feedback) => feedback.draftId === draft.id).length + 1;
      const feedback = CustomerDraftFeedbackSchema.parse({
        id: `customer_feedback_${stableDigest({
          scope: scopedIdentity(scope),
          operationKey: operationValue,
        })}`,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version,
        demandId: brief.demandId,
        briefId: brief.id,
        ...normalized,
        actorId: scope.actorId,
        publishClaimed: false,
        createdAt: this.timestamp(),
      });
      this.customerFeedback.set(feedback.id, feedback);
      this.currentCustomerFeedbackByDraft.set(draft.id, feedback.id);
      this.customerFeedbackOperations.set(operationKey, {
        actorId: scope.actorId,
        bodyDigest,
        value: feedback,
      });
      return clone(feedback);
    });
  }

  async proposeRevision(
    scope: StudioScope,
    feedbackIdValue: string,
    operationKeyValue: string,
  ): Promise<RevisionProposal> {
    assertScope(scope);
    assertText(feedbackIdValue, "customer-feedback-id-required");
    assertText(operationKeyValue, "revision-proposal-operation-key-required");
    const feedbackId = feedbackIdValue.trim();
    const operationValue = normalizeOperationKey(operationKeyValue, "revision-proposal-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    const bodyDigest = stableDigest({ feedbackId });
    return this.withScopeLock(scope, async () => {
      const existing = this.revisionProposalOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) {
        throw new Error("revision-proposal-operation-actor-mismatch");
      }
      if (existing && existing.bodyDigest !== bodyDigest) {
        throw new Error("revision-proposal-operation-key-conflict");
      }
      const feedback = this.requireScopedCustomerFeedback(scope, feedbackId);
      if (this.currentCustomerFeedbackByDraft.get(feedback.draftId) !== feedback.id) {
        throw new Error("customer-feedback-not-current");
      }
      if (feedback.satisfaction === "satisfied") throw new Error("feedback-not-revision-request");
      const draft = this.requireScopedDraft(scope, feedback.draftId);
      const brief = this.requireScopedBrief(scope, feedback.briefId);
      if (draft.briefId !== brief.id || draft.demandId !== feedback.demandId ||
          brief.demandId !== feedback.demandId) {
        throw new Error("customer-feedback-lineage-mismatch");
      }
      await this.assertCustomerFeedbackDraftCurrent(scope, draft);
      if (existing) return clone(this.deriveRevisionProposal(existing.value));

      const versionKey = feedback.id;
      const version = (this.revisionProposalVersions.get(versionKey) ?? 0) + 1;
      const proposal = buildOfflineRevisionProposal({
        identity: {
          id: `revision_proposal_${stableDigest({
            scope: scopedIdentity(scope),
            feedbackId,
            operationKey: operationValue,
          })}`,
          customerId: scope.customerId,
          sessionId: scope.sessionId,
          version,
          demandId: feedback.demandId,
          feedbackId: feedback.id,
          parentDraftId: draft.id,
          parentBriefId: brief.id,
          capability: snapshotCapability(this.understanding.capability),
        },
        feedback: clone(feedback),
        createdAt: this.timestamp(),
      });
      this.assertImmutableRevisionProposalSlot(proposal, scope.actorId);
      this.revisionProposals.set(proposal.id, proposal);
      this.revisionProposalActors.set(proposal.id, scope.actorId);
      this.revisionProposalOperations.set(operationKey, {
        actorId: scope.actorId,
        bodyDigest,
        value: proposal,
      });
      this.revisionProposalVersions.set(versionKey, version);
      this.currentRevisionProposals.set(versionKey, proposal.id);
      return clone(proposal);
    });
  }

  async getRevisionProposal(scope: StudioScope, proposalIdValue: string): Promise<RevisionProposal> {
    assertScope(scope);
    assertText(proposalIdValue, "revision-proposal-id-required");
    return clone(this.deriveRevisionProposal(
      this.requireScopedRevisionProposal(scope, proposalIdValue.trim()),
    ));
  }

  async confirmRevisionProposal(
    scope: StudioScope,
    proposalIdValue: string,
    operationKeyValue: string,
  ): Promise<StudioCreativeBrief> {
    assertScope(scope);
    assertText(proposalIdValue, "revision-proposal-id-required");
    assertText(operationKeyValue, "revision-confirmation-operation-key-required");
    const proposalId = proposalIdValue.trim();
    const operationValue = normalizeOperationKey(operationKeyValue, "revision-confirmation-operation-key-too-long");
    const operationKey = `${scope.customerId}\0${scope.sessionId}\0${operationValue}`;
    const bodyDigest = stableDigest({ proposalId });
    return this.withScopeLock(scope, async () => {
      const existing = this.revisionConfirmationOperations.get(operationKey);
      if (existing && existing.actorId !== scope.actorId) {
        throw new Error("revision-confirmation-operation-actor-mismatch");
      }
      if (existing && existing.bodyDigest !== bodyDigest) {
        throw new Error("revision-confirmation-operation-key-conflict");
      }
      if (existing) {
        const revised = this.requireScopedBrief(scope, existing.value.id);
        this.assertBriefCurrent(revised);
        await this.assertBriefDemandMatchCurrent(scope, revised);
        return clone(revised);
      }

      const storedProposal = this.requireScopedRevisionProposal(scope, proposalId);
      const proposal = this.deriveRevisionProposal(storedProposal);
      if (proposal.status !== "proposed") throw new Error("revision-proposal-not-current");
      if (this.currentRevisionProposals.get(proposal.feedbackId) !== proposal.id ||
          this.revisionProposalVersions.get(proposal.feedbackId) !== proposal.version) {
        throw new Error("revision-proposal-version-not-current");
      }
      if (proposal.unsupportedRequests.length > 0) {
        throw new Error("revision-proposal-has-unsupported-requests");
      }
      if (proposal.captureGaps.length > 0) {
        throw new Error("revision-proposal-has-unresolved-gaps");
      }
      const feedback = this.requireScopedCustomerFeedback(scope, proposal.feedbackId);
      const draft = this.requireScopedDraft(scope, proposal.parentDraftId);
      const parent = this.requireScopedBrief(scope, proposal.parentBriefId);
      if (feedback.draftId !== draft.id || feedback.briefId !== parent.id ||
          feedback.demandId !== proposal.demandId || draft.briefId !== parent.id ||
          parent.demandId !== proposal.demandId) {
        throw new Error("revision-proposal-lineage-mismatch");
      }
      this.assertBriefCurrent(parent);
      await this.assertCustomerFeedbackDraftCurrent(scope, draft);
      const versionKey = this.briefVersionKey(parent);
      if (this.briefVersions.get(versionKey) !== parent.version) {
        throw new Error("brief-version-not-current");
      }
      const confirmedAt = this.timestamp();
      const interpretation = interpretOfflineStudioFeedback(feedback.comment ?? "");
      const reviewRequirements = normalizeUnique([
        ...parent.reviewRequirements,
        `客户反馈确认：${proposal.understoodFeedback}`,
        ...proposal.plannedChanges.map((change) => `已确认修改：${change}`),
        ...(interpretation.adjustments.length > 0
          ? [
              OFFLINE_FEEDBACK_INTERPRETER_REQUIREMENT,
              ...interpretation.adjustments.map((adjustment) =>
                `${OFFLINE_FEEDBACK_RULE_PREFIX}${adjustment}`),
            ]
          : []),
      ], "review-requirement-required");
      const revised = StudioCreativeBriefSchema.parse({
        ...parent,
        id: `brief_${stableDigest({
          kind: "confirmed-revision-proposal",
          scope: scopedIdentity(scope),
          proposalId: proposal.id,
          operationKey: operationValue,
        })}`,
        version: parent.version + 1,
        parentBriefId: parent.id,
        feedbackId: feedback.id,
        revisionProposalId: proposal.id,
        reviewRequirements,
        createdAt: confirmedAt,
      });
      this.assertImmutableBriefSlot(revised, scope.actorId);
      const receipt: RevisionConfirmationReceipt = {
        actorId: scope.actorId,
        proposalId: proposal.id,
        confirmedAt,
      };
      this.revisionConfirmationReceipts.set(proposal.id, receipt);
      this.briefVersions.set(versionKey, revised.version);
      this.briefs.set(revised.id, revised);
      this.briefActors.set(revised.id, scope.actorId);
      this.currentBriefs.set(versionKey, revised.id);
      this.revisionConfirmationOperations.set(operationKey, {
        actorId: scope.actorId,
        bodyDigest,
        value: revised,
      });
      return clone(revised);
    });
  }

  async listStudioHistory(scope: StudioScope): Promise<StudioHistory> {
    assertScope(scope);
    const inScope = (value: { customerId: string; sessionId: string }) =>
      value.customerId === scope.customerId && value.sessionId === scope.sessionId;
    const actorOwns = (actors: Map<string, string>, id: string) => actors.get(id) === scope.actorId;
    const demands = [...this.demands.values()]
      .filter((value) => inScope(value) && actorOwns(this.demandActors, value.id))
      .map((value) => ({
        ...value,
        status: this.currentDemands.get(this.demandVersionKey(scope)) === value.id
          ? "current" as const
          : "superseded" as const,
      }));
    const assessments = [...this.matches.values()]
      .filter((stored) => inScope(stored.value) && stored.actorId === scope.actorId)
      .map((stored) => stored.value);
    const opportunities = [...this.opportunities.values()]
      .filter((stored) => inScope(stored.value) && stored.actorId === scope.actorId)
      .map((stored) => stored.value);
    const briefs = [...this.briefs.values()]
      .filter((value) => inScope(value) && actorOwns(this.briefActors, value.id));
    const scopedDrafts = [...this.drafts.values()]
      .filter((value) => inScope(value) && actorOwns(this.draftActors, value.id));
    // Inventory is stored as many small, integrity-checked records. Reading it
    // once per historical draft made an old session progressively slower.
    // Resolve source currency once, then verify independent render files in
    // parallel. Sensitive actions and media reads still hash the exact bytes.
    const sourceCurrency = await this.draftSourceCurrency(scope, scopedDrafts);
    const drafts = await Promise.all(scopedDrafts.map(async (draft) => {
      if (!sourceCurrency.get(draft.id) || !await this.isDraftOutputCurrent(draft)) {
        return clone({ ...draft, status: "invalidated" as const });
      }
      return clone(this.deriveDraftStatus(draft));
    }));
    const customerFeedback = [...this.customerFeedback.values()]
      .filter((value) => inScope(value) && value.actorId === scope.actorId);
    const revisionProposals = [...this.revisionProposals.values()]
      .filter((value) => inScope(value) && actorOwns(this.revisionProposalActors, value.id))
      .map((value) => this.deriveRevisionProposal(value));
    const distributionEvents = [...this.distributionEvents.values()]
      .filter((value) => inScope(value) && value.actorId === scope.actorId);
    return clone({
      demands: sortVersioned(demands),
      assessments: sortVersioned(assessments),
      opportunities: sortVersioned(opportunities),
      briefs: sortVersioned(briefs),
      drafts: sortVersioned(drafts),
      customerFeedback: sortVersioned(customerFeedback),
      revisionProposals: sortVersioned(revisionProposals),
      distributionEvents: sortVersioned(distributionEvents),
    });
  }

  /**
   * A local, customer-scoped workflow ledger. Media bytes remain owned by the
   * material library; this snapshot only preserves the editorial state needed
   * to continue after the local service restarts.
   */
  async exportDurableState(scope: StudioScope): Promise<StudioDurableStateV1> {
    const history = await this.listStudioHistory(scope);
    const feedback = [...this.feedback.values()]
      .filter((event) => event.actorId === scope.actorId
        && event.customerId === scope.customerId
        && event.sessionId === scope.sessionId)
      .sort((left, right) => left.version - right.version);
    return clone({ schemaVersion: 1 as const, history, feedback });
  }

  async restoreDurableState(scope: StudioScope, stateValue: unknown): Promise<void> {
    assertScope(scope);
    if (!isRecordValue(stateValue) || stateValue.schemaVersion !== 1
        || !isRecordValue(stateValue.history) || !Array.isArray(stateValue.feedback)) {
      throw new Error("studio-durable-state-invalid");
    }
    const historyValue = stateValue.history;
    const requiredHistoryKeys = [
      "demands", "assessments", "opportunities", "briefs", "drafts",
      "customerFeedback", "revisionProposals",
    ] as const;
    const allowedHistoryKeys = [...requiredHistoryKeys, "distributionEvents"];
    if (Object.keys(historyValue).some((key) => !allowedHistoryKeys.includes(key as typeof allowedHistoryKeys[number]))
        || requiredHistoryKeys.some((key) => !Array.isArray(historyValue[key]))
        || (historyValue.distributionEvents !== undefined && !Array.isArray(historyValue.distributionEvents))) {
      throw new Error("studio-durable-state-invalid");
    }
    const historyArrays = historyValue as Record<typeof requiredHistoryKeys[number], unknown[]>;
    const history: StudioHistory = {
      demands: historyArrays.demands.map((value) => ContentDemandSchema.parse(value)),
      assessments: historyArrays.assessments.map((value) => MatchAssessmentSchema.parse(value)),
      opportunities: historyArrays.opportunities.map((value) => ContentOpportunitySchema.parse(value)),
      briefs: historyArrays.briefs.map((value) => StudioCreativeBriefSchema.parse(value)),
      drafts: historyArrays.drafts.map((value) => DraftPackageSchema.parse(value)),
      customerFeedback: historyArrays.customerFeedback.map((value) => CustomerDraftFeedbackSchema.parse(value)),
      revisionProposals: historyArrays.revisionProposals.map((value) => RevisionProposalSchema.parse(value)),
      distributionEvents: Array.isArray(historyValue.distributionEvents)
        ? historyValue.distributionEvents.map((value) => StudioDistributionEventSchema.parse(value))
        : [],
    };
    const feedback = stateValue.feedback.map((value) => FeedbackEventSchema.parse(value));
    const inScope = (value: { customerId: string; sessionId: string }) =>
      value.customerId === scope.customerId && value.sessionId === scope.sessionId;
    if ([
      ...history.demands,
      ...history.assessments,
      ...history.opportunities,
      ...history.briefs,
      ...history.drafts,
      ...history.customerFeedback,
      ...history.revisionProposals,
      ...history.distributionEvents,
      ...feedback,
    ].some((value) => !inScope(value))) {
      throw new Error("studio-durable-state-scope-mismatch");
    }
    if ([...history.customerFeedback, ...feedback]
      .some((value) => value.actorId !== scope.actorId)) {
      throw new Error("studio-durable-state-actor-mismatch");
    }

    const currentCards = latestCardPerAsset(
      await this.library.inventoryAt(
        scope.actorId, scope.customerId, this.timestamp(), scope.sessionId,
      ),
    );
    const cardsById = new Map(currentCards.map((card) => [card.id, card]));
    const inventoryCardIdentity = stableDigest(cardIdentity(currentCards));

    for (const demand of history.demands) {
      this.demands.set(demand.id, demand);
      this.demandActors.set(demand.id, scope.actorId);
    }
    const currentDemand = [...history.demands].sort((left, right) => right.version - left.version)[0];
    if (currentDemand) {
      const key = this.demandVersionKey(scope);
      this.demandVersions.set(key, currentDemand.version);
      this.currentDemands.set(key, currentDemand.id);
    }
    for (const assessment of history.assessments) {
      this.matches.set(assessment.id, {
        actorId: scope.actorId,
        value: assessment,
        inventoryCardIdentity,
        gapList: null,
      });
    }
    for (const opportunity of history.opportunities) {
      this.opportunities.set(this.opportunityKey(scope, opportunity.id), {
        actorId: scope.actorId,
        value: opportunity,
        cards: opportunity.materialCardIds
          .map((cardId) => cardsById.get(cardId))
          .filter((card): card is MaterialCard => card !== undefined),
      });
    }
    for (const brief of history.briefs) {
      this.briefs.set(brief.id, brief);
      this.briefActors.set(brief.id, scope.actorId);
      const key = this.briefVersionKey(brief);
      const priorVersion = this.briefVersions.get(key) ?? 0;
      if (brief.version >= priorVersion) {
        this.briefVersions.set(key, brief.version);
        this.currentBriefs.set(key, brief.id);
      }
    }
    for (const draft of history.drafts) {
      this.drafts.set(draft.id, draft);
      this.draftActors.set(draft.id, scope.actorId);
    }
    for (const event of feedback) this.feedback.set(event.id, event);
    for (const event of history.distributionEvents) this.distributionEvents.set(event.id, event);
    for (const customerFeedback of history.customerFeedback) {
      this.customerFeedback.set(customerFeedback.id, customerFeedback);
      const priorId = this.currentCustomerFeedbackByDraft.get(customerFeedback.draftId);
      const prior = priorId ? this.customerFeedback.get(priorId) : undefined;
      if (!prior || customerFeedback.version >= prior.version) {
        this.currentCustomerFeedbackByDraft.set(customerFeedback.draftId, customerFeedback.id);
      }
    }
    for (const proposal of history.revisionProposals) {
      this.revisionProposals.set(proposal.id, proposal);
      this.revisionProposalActors.set(proposal.id, scope.actorId);
      const priorVersion = this.revisionProposalVersions.get(proposal.feedbackId) ?? 0;
      if (proposal.version >= priorVersion) {
        this.revisionProposalVersions.set(proposal.feedbackId, proposal.version);
        this.currentRevisionProposals.set(proposal.feedbackId, proposal.id);
      }
      if (proposal.confirmedAt && proposal.confirmedByActorId) {
        this.revisionConfirmationReceipts.set(proposal.id, {
          actorId: proposal.confirmedByActorId,
          proposalId: proposal.id,
          confirmedAt: proposal.confirmedAt,
        });
      }
    }
  }

  private requirePreferenceRanker(): StudioPreferenceRanker {
    if (!this.preferenceRanker) throw new Error("studio-preference-ranker-unavailable");
    return this.preferenceRanker;
  }

  private async reconcileDraftPreferences(scope: StudioScope): Promise<void> {
    const events = [...this.feedback.values()]
      .filter((event) =>
        event.actorId === scope.actorId
        && event.customerId === scope.customerId
        && event.sessionId === scope.sessionId
        && event.action !== "request-changes")
      .sort((left, right) => compareCodePoints(left.id, right.id));
    for (const event of events) await this.commitDraftFeedback(scope, event);
    await this.preferenceRanker?.reconcilePersistedDraftFeedback(scope);
  }

  private async recordDraftPreference(scope: StudioScope, event: FeedbackEvent): Promise<void> {
    await this.commitDraftFeedback(scope, event);
    await this.preferenceRanker?.reconcilePersistedDraftFeedback(scope);
  }

  private async commitDraftFeedback(scope: StudioScope, event: FeedbackEvent): Promise<void> {
    if (!this.preferenceRanker || event.action === "request-changes") return;
    const draft = this.requireScopedDraft(scope, event.draftId);
    await this.preferenceRanker.commitCanonicalDraftFeedback(scope, {
      operationKey: `draft-feedback:${event.id}`,
      eventId: event.id,
      action: event.action,
      draftId: draft.id,
      demandId: draft.demandId,
      sources: draft.sourceMap.map((source) => ({
        sourceAssetId: source.sourceAssetId,
        sourceDigest: source.sourceDigest,
        sourceStartMs: source.sourceStartMs,
        sourceEndMs: source.sourceEndMs,
      })),
      createdAt: event.createdAt,
    }, event);
  }

  async adoptDraft(scope: StudioScope, draftId: string, operationKey: string): Promise<FeedbackEvent> {
    return this.recordFeedback(scope, { draftId, action: "adopt", reason: null, operationKey });
  }

  async reviseBrief(
    scope: StudioScope,
    briefId: string,
    feedbackEventId: string,
    operationKeyValue: string,
  ): Promise<StudioCreativeBrief> {
    assertScope(scope);
    assertText(briefId, "brief-id-required");
    assertText(feedbackEventId, "feedback-event-id-required");
    assertText(operationKeyValue, "brief-operation-key-required");
    throw new Error("structured-revision-confirmation-required");
  }

  private async inventoryWithCards(scope: StudioScope, asOf: string): Promise<{
    inventory: MaterialInventory;
    cards: MaterialCard[];
  }> {
    assertScope(scope);
    const cards = latestCardPerAsset(
      await this.library.inventoryAt(scope.actorId, scope.customerId, asOf, scope.sessionId),
    );
    const inventory = MaterialInventorySchema.parse({
      id: `inventory_${stableDigest({ scope: scopedIdentity(scope), asOf, cards: cardIdentity(cards) })}`,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 1,
      asOf,
      items: cards.map((card) => ({
        materialCardId: card.id,
        sourceAssetId: card.sourceAssetId,
        selectionReason: card.technicalUsable
          ? `客户授权有效；${card.contentType} 素材技术可用`
          : `客户授权有效；${card.contentType} 素材保留但技术不可用`,
      })),
      capability: capabilityForCards(
        cards,
        this.evidenceByCardId,
        this.degradedReasonsByCardId,
        this.providerIdentitiesByCardId,
        this.understanding.capability,
      ),
    });
    return clone({ inventory, cards });
  }

  private timestamp(): string {
    const value = this.now();
    if (!Number.isFinite(Date.parse(value))) throw new Error("studio-clock-invalid");
    return value;
  }

  private opportunityKey(scope: StudioScope, opportunityId: string): string {
    return `${scope.customerId}\0${scope.sessionId}\0${opportunityId}`;
  }

  private demandVersionKey(scope: StudioScope): string {
    return `${scope.customerId}\0${scope.sessionId}`;
  }

  private requireScopedDemand(scope: StudioScope, demandId: string): ContentDemand {
    const demand = this.demands.get(demandId);
    if (!demand || demand.customerId !== scope.customerId || demand.sessionId !== scope.sessionId) {
      throw new Error("demand-not-found");
    }
    if (this.demandActors.get(demandId) !== scope.actorId) throw new Error("demand-actor-mismatch");
    return demand;
  }

  private assertDemandCurrent(scope: StudioScope, demand: ContentDemand): void {
    const versionKey = this.demandVersionKey(scope);
    if (this.currentDemands.get(versionKey) !== demand.id ||
        this.demandVersions.get(versionKey) !== demand.version) {
      throw new Error("demand-version-not-current");
    }
  }

  private requireScopedMatch(scope: StudioScope, assessmentId: string): StoredMatch {
    const stored = this.matches.get(assessmentId);
    if (!stored || stored.value.customerId !== scope.customerId || stored.value.sessionId !== scope.sessionId) {
      throw new Error("match-assessment-not-found");
    }
    if (stored.actorId !== scope.actorId) throw new Error("match-assessment-actor-mismatch");
    return stored;
  }

  private assertMatchInventoryCurrent(stored: StoredMatch, cards: readonly MaterialCard[]): void {
    if (stableDigest(cardIdentity(cards)) !== stored.inventoryCardIdentity) {
      throw new Error("match-inventory-not-current");
    }
  }

  private async assertBriefDemandMatchCurrent(
    scope: StudioScope,
    brief: StudioCreativeBrief,
  ): Promise<void> {
    const demand = this.requireScopedDemand(scope, brief.demandId);
    this.assertDemandCurrent(scope, demand);
    const storedMatch = this.requireScopedMatch(scope, brief.matchAssessmentId);
    if (storedMatch.value.demandId !== demand.id) throw new Error("match-demand-mismatch");
    if (storedMatch.value.verdict !== "ready") throw new Error("match-not-ready");
    const current = await this.inventoryWithCards(scope, this.timestamp());
    this.assertMatchInventoryCurrent(storedMatch, current.cards);
  }

  private requireScopedBrief(scope: StudioScope, briefId: string): StudioCreativeBrief {
    const brief = this.briefs.get(briefId);
    if (!brief || brief.customerId !== scope.customerId || brief.sessionId !== scope.sessionId) {
      throw new Error("brief-not-found");
    }
    if (this.briefActors.get(briefId) !== scope.actorId) throw new Error("brief-actor-mismatch");
    return brief;
  }

  private assertImmutableBriefSlot(brief: StudioCreativeBrief, actorId: string): void {
    const existing = this.briefs.get(brief.id);
    if (existing && (this.briefActors.get(brief.id) !== actorId ||
        stableDigest(existing) !== stableDigest(brief))) {
      throw new Error("immutable-brief-id-conflict");
    }
  }

  private requireScopedDraft(scope: StudioScope, draftId: string): DraftPackage {
    assertText(draftId, "draft-id-required");
    const draft = this.drafts.get(draftId);
    if (!draft || draft.customerId !== scope.customerId || draft.sessionId !== scope.sessionId) {
      throw new Error("draft-not-found");
    }
    if (this.draftActors.get(draftId) !== scope.actorId) throw new Error("draft-actor-mismatch");
    return draft;
  }

  private requireScopedCustomerFeedback(
    scope: StudioScope,
    feedbackId: string,
  ): CustomerDraftFeedback {
    const feedback = this.customerFeedback.get(feedbackId);
    if (!feedback || feedback.customerId !== scope.customerId || feedback.sessionId !== scope.sessionId) {
      throw new Error("customer-feedback-not-found");
    }
    if (feedback.actorId !== scope.actorId) throw new Error("customer-feedback-actor-mismatch");
    return feedback;
  }

  private requireScopedRevisionProposal(scope: StudioScope, proposalId: string): RevisionProposal {
    const proposal = this.revisionProposals.get(proposalId);
    if (!proposal || proposal.customerId !== scope.customerId || proposal.sessionId !== scope.sessionId) {
      throw new Error("revision-proposal-not-found");
    }
    if (this.revisionProposalActors.get(proposalId) !== scope.actorId) {
      throw new Error("revision-proposal-actor-mismatch");
    }
    return proposal;
  }

  private assertImmutableRevisionProposalSlot(proposal: RevisionProposal, actorId: string): void {
    const existing = this.revisionProposals.get(proposal.id);
    if (existing && (this.revisionProposalActors.get(proposal.id) !== actorId ||
        stableDigest(existing) !== stableDigest(proposal))) {
      throw new Error("immutable-revision-proposal-id-conflict");
    }
  }

  private deriveRevisionProposal(proposal: RevisionProposal): RevisionProposal {
    const receipt = this.revisionConfirmationReceipts.get(proposal.id);
    const currentId = this.currentRevisionProposals.get(proposal.feedbackId);
    const feedback = this.customerFeedback.get(proposal.feedbackId);
    const feedbackIsCurrent = feedback !== undefined &&
      this.currentCustomerFeedbackByDraft.get(feedback.draftId) === feedback.id;
    const revised = [...this.briefs.values()].some((brief) => brief.revisionProposalId === proposal.id);
    const parent = this.briefs.get(proposal.parentBriefId);
    const parentIsCurrent = parent !== undefined &&
      this.currentBriefs.get(this.briefVersionKey(parent)) === parent.id &&
      this.briefVersions.get(this.briefVersionKey(parent)) === parent.version &&
      this.currentDemands.get(`${proposal.customerId}\0${proposal.sessionId}`) === proposal.demandId;
    const status: RevisionProposal["status"] = receipt
      ? revised ? "executed" : "confirmed"
      : currentId === proposal.id && feedbackIsCurrent && parentIsCurrent ? "proposed" : "superseded";
    return RevisionProposalSchema.parse({
      ...proposal,
      status,
      confirmedByActorId: receipt?.actorId ?? null,
      confirmedAt: receipt?.confirmedAt ?? null,
    });
  }

  private async assertCustomerFeedbackDraftCurrent(
    scope: StudioScope,
    draft: DraftPackage,
  ): Promise<void> {
    await this.assertDraftSourcesCurrent(scope, [draft], "draft-source-withdrawn");
    await this.assertDraftOutputCurrent(draft);
    const brief = this.requireScopedBrief(scope, draft.briefId);
    this.assertBriefCurrent(brief);
    await this.assertBriefDemandMatchCurrent(scope, brief);
    if (draft.demandId !== brief.demandId || draft.version !== brief.version) {
      throw new Error("draft-brief-lineage-mismatch");
    }
  }

  private assertSatisfiedCustomerFeedback(draft: DraftPackage): void {
    const latest = [...this.customerFeedback.values()]
      .filter((feedback) => feedback.draftId === draft.id)
      .sort((left, right) => left.version - right.version)
      .at(-1);
    if (!latest || latest.satisfaction !== "satisfied") {
      throw new Error("satisfied-customer-feedback-required");
    }
    assertCustomerFeedbackConsistency(latest);
  }

  private briefVersionKey(brief: StudioCreativeBrief): string {
    return `${brief.customerId}\0${brief.sessionId}\0${brief.opportunityId}`;
  }

  private assertBriefCurrent(brief: StudioCreativeBrief): void {
    const versionKey = this.briefVersionKey(brief);
    if (this.briefVersions.get(versionKey) !== brief.version || this.currentBriefs.get(versionKey) !== brief.id) {
      throw new Error("brief-version-not-current");
    }
  }

  private async currentRenderInputs(
    scope: StudioScope,
    brief: StudioCreativeBrief,
    withdrawalError: string,
  ): Promise<{ cards: MaterialCard[]; sources: StudioRenderSource[] }> {
    let cards: MaterialCard[];
    let assets: StudioMediaAsset[];
    try {
      [cards, assets] = await Promise.all([
        this.library.inventoryAt(scope.actorId, scope.customerId, this.timestamp(), scope.sessionId),
        this.library.listAssets(scope.actorId, scope.customerId),
      ]);
    } catch (error) {
      if (isWithdrawalError(error)) throw new Error(withdrawalError);
      throw error;
    }
    const cardByAsset = new Map(
      latestCardPerAsset(cards.filter((card) => card.sessionId === scope.sessionId))
        .map((card) => [card.sourceAssetId, card]),
    );
    const assetById = new Map(assets.map((asset) => [asset.id, asset]));
    const selectedCards: MaterialCard[] = [];
    const sources: StudioRenderSource[] = [];
    for (const assetId of brief.selectedAssetIds) {
      const card = cardByAsset.get(assetId);
      const asset = assetById.get(assetId);
      if (!card || !asset || !card.technicalUsable || asset.usePermission !== "allowed" ||
          asset.importBindingId !== assetId || asset.blobDigest !== card.sourceDigest ||
          asset.digest !== card.sourceDigest) {
        throw new Error(withdrawalError);
      }
      let bytes: Buffer;
      try {
        bytes = await this.library.readOriginal(scope.actorId, scope.customerId, assetId);
      } catch (error) {
        if (isWithdrawalError(error)) throw new Error(withdrawalError);
        throw error;
      }
      if (stableBytesDigest(bytes) !== asset.blobDigest) throw new Error("draft-source-digest-mismatch");
      selectedCards.push(card);
      sources.push({ assetId: asset.importBindingId, blobDigest: asset.blobDigest, bytes });
    }
    return { cards: selectedCards, sources };
  }

  private async areDraftSourcesCurrent(scope: StudioScope, drafts: readonly DraftPackage[]): Promise<boolean> {
    const currency = await this.draftSourceCurrency(scope, drafts);
    return drafts.every((draft) => currency.get(draft.id) === true);
  }

  private async draftSourceCurrency(
    scope: StudioScope,
    drafts: readonly DraftPackage[],
  ): Promise<Map<string, boolean>> {
    try {
      const [cards, assets] = await Promise.all([
        this.library.inventoryAt(scope.actorId, scope.customerId, this.timestamp(), scope.sessionId),
        this.library.listAssets(scope.actorId, scope.customerId),
      ]);
      const current = new Map(cards
        .filter((card) => card.sessionId === scope.sessionId)
        .map((card) => [card.sourceAssetId, card.sourceDigest]));
      const currentMusic = new Map(assets
        .filter((asset) => asset.sessionId === scope.sessionId && asset.mediaKind === "audio" && asset.usePermission === "allowed")
        .map((asset) => [asset.id, asset.digest]));
      return new Map(drafts.map((draft) => [
        draft.id,
        draft.sourceMap.every((row) => current.get(row.sourceAssetId) === row.sourceDigest) &&
          (!draft.musicMixPlan || currentMusic.get(draft.musicMixPlan.trackAssetId) === draft.musicMixPlan.trackDigest),
      ]));
    } catch (error) {
      if (isWithdrawalError(error)) {
        return new Map(drafts.map((draft) => [draft.id, false]));
      }
      throw error;
    }
  }

  private async assertDraftSourcesCurrent(
    scope: StudioScope,
    drafts: readonly DraftPackage[],
    error: string,
  ): Promise<void> {
    if (!await this.areDraftSourcesCurrent(scope, drafts)) throw new Error(error);
  }

  private async isDraftOutputCurrent(draft: DraftPackage): Promise<boolean> {
    try {
      const { outputPath, metadata } = await this.resolveDraftOutput(draft);
      const cached = this.verifiedDraftOutputs.get(draft.id);
      if (cached
          && cached.outputPath === outputPath
          && cached.outputDigest === draft.outputDigest
          && cached.size === metadata.size
          && cached.mtimeMs === metadata.mtimeMs
          && cached.ctimeMs === metadata.ctimeMs) {
        return true;
      }
      const bytes = await readFile(outputPath);
      if (stableBytesDigest(bytes) !== draft.outputDigest) return false;
      this.verifiedDraftOutputs.set(draft.id, {
        outputPath,
        outputDigest: draft.outputDigest,
        size: metadata.size,
        mtimeMs: metadata.mtimeMs,
        ctimeMs: metadata.ctimeMs,
      });
      return true;
    } catch {
      return false;
    }
  }

  private async assertDraftOutputCurrent(draft: DraftPackage): Promise<void> {
    await this.readVerifiedDraftOutput(draft);
  }

  private async readVerifiedDraftOutput(draft: DraftPackage): Promise<Buffer> {
    const { outputPath, metadata } = await this.resolveDraftOutput(draft);
    const bytes = await readFile(outputPath).catch(() => {
      throw new Error("studio-render-output-unavailable");
    });
    if (stableBytesDigest(bytes) !== draft.outputDigest) {
      throw new Error("studio-render-output-digest-mismatch");
    }
    this.verifiedDraftOutputs.set(draft.id, {
      outputPath,
      outputDigest: draft.outputDigest,
      size: metadata.size,
      mtimeMs: metadata.mtimeMs,
      ctimeMs: metadata.ctimeMs,
    });
    return bytes;
  }

  private async resolveDraftOutput(draft: DraftPackage): Promise<{
    outputPath: string;
    metadata: Readonly<{ size: number; mtimeMs: number; ctimeMs: number }>;
  }> {
    const outputRoot = this.outputRoot;
    if (!outputRoot) throw new Error("studio-render-output-unavailable");
    const outputPath = resolve(draft.outputPath);
    if (outputPath !== draft.outputPath) {
      throw new Error("studio-render-output-outside-root");
    }

    const [realOutputRoot, realOutputPath] = await Promise.all([
      realpath(outputRoot),
      realpath(outputPath),
    ]).catch(() => {
      throw new Error("studio-render-output-unavailable");
    });
    if (!isContainedPath(realOutputRoot, realOutputPath)) {
      throw new Error("studio-render-output-outside-root");
    }
    const metadata = await stat(realOutputPath).catch(() => null);
    if (!metadata?.isFile() || metadata.size === 0) {
      throw new Error("studio-render-output-unavailable");
    }
    return {
      outputPath: realOutputPath,
      metadata: {
        size: Number(metadata.size),
        mtimeMs: Number(metadata.mtimeMs),
        ctimeMs: Number(metadata.ctimeMs),
      },
    };
  }

  private deriveDraftStatus(draft: DraftPackage): DraftPackage {
    const adoptedForBrief = !draft.shareOptimization && [...this.feedback.values()].find((event) => {
      if (event.action !== "adopt") return false;
      const adoptedDraft = this.drafts.get(event.draftId);
      return adoptedDraft !== undefined
        && !adoptedDraft.shareOptimization
        && adoptedDraft.briefId === draft.briefId
        && adoptedDraft.version === draft.version;
    });
    if (adoptedForBrief) {
      return { ...draft, status: adoptedForBrief.draftId === draft.id ? "adopted" : "rejected" };
    }
    const latestCustomerFeedback = [...this.customerFeedback.values()]
      .filter((feedback) => feedback.draftId === draft.id)
      .sort((left, right) => left.version - right.version)
      .at(-1);
    if (latestCustomerFeedback && latestCustomerFeedback.satisfaction !== "satisfied") {
      return { ...draft, status: "changes-requested" };
    }
    const events = [...this.feedback.values()]
      .filter((event) => event.draftId === draft.id)
      .sort((left, right) => left.version - right.version);
    const latest = events.at(-1);
    if (!latest) return draft;
    const status = latest.action === "adopt"
      ? "adopted"
      : latest.action === "reject"
        ? "rejected"
        : "changes-requested";
    return { ...draft, status };
  }

  private async withScopeLock<T>(scope: StudioScope, work: () => Promise<T>): Promise<T> {
    const key = `${scope.customerId}\0${scope.sessionId}`;
    const previous = this.scopeQueues.get(key) ?? Promise.resolve();
    let release!: () => void;
    const gate = new Promise<void>((resolveGate) => { release = resolveGate; });
    const tail = previous.then(() => gate, () => gate);
    this.scopeQueues.set(key, tail);
    await previous.catch(() => undefined);
    try {
      return await work();
    } finally {
      release();
      if (this.scopeQueues.get(key) === tail) this.scopeQueues.delete(key);
    }
  }
}

function isContainedPath(root: string, candidate: string): boolean {
  const child = relative(root, candidate);
  return child.length > 0 && child !== ".." && !child.startsWith(`..${sep}`) && !isAbsolute(child);
}

class OfflineEditorialDecisionAdapter implements EditorialDecisionAdapter {
  get capability() {
    return OFFLINE_STUDIO_CAPABILITY;
  }

  async createBlueprints(input: Parameters<EditorialDecisionAdapter["createBlueprints"]>[0]): Promise<readonly EditBlueprint[]> {
    if (input.cards.length === 1 && supportsSingleSourceDraftDirections(input.cards[0]!)) {
      return STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES.map((structure, index) =>
        makeSingleSourceBlueprint(
          input.scope,
          input.brief,
          input.cards[0]!,
          structure,
          input.createdAt,
          index === 1,
        ));
    }
    if (input.cards.length < 3) throw new Error("three-renderable-cards-required");
    const evidenceFirst = input.cards.slice(0, 3);
    const viewpointFirst = input.cards.length >= 4
      ? [input.cards[3]!, input.cards[1]!, input.cards[0]!]
      : [input.cards[2]!, input.cards[0]!, input.cards[1]!];
    return [
      makeBlueprint(
        input.scope,
        input.brief,
        evidenceFirst,
        STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES[0],
        input.createdAt,
        offlineAdjustmentsFromBrief(input.brief),
      ),
      makeBlueprint(
        input.scope,
        input.brief,
        viewpointFirst,
        STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES[1],
        input.createdAt,
        offlineAdjustmentsFromBrief(input.brief),
      ),
    ];
  }
}

function makeSingleSourceBlueprint(
  scope: StudioScope,
  brief: StudioCreativeBrief,
  card: MaterialCard,
  structure: (typeof STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES)[number],
  createdAt: string,
  reverse: boolean,
): EditBlueprint {
  const sourceDurationMs = card.sourceEndMs - card.sourceStartMs;
  const shotDurationMs = Math.min(20_000, Math.max(10_000, Math.floor(sourceDurationMs * 0.6)));
  const availableOffsetMs = sourceDurationMs - shotDurationMs;
  const offsets = [0, Math.floor(availableOffsetMs / 2), availableOffsetMs];
  if (reverse) offsets.reverse();
  let timelineStartMs = 0;
  const shots = offsets.map((offset, index) => {
    const sourceStartMs = card.sourceStartMs + offset;
    const shot = {
      id: `shot_${stableDigest({ briefId: brief.id, direction: structure.direction, index, sourceStartMs })}`,
      sourceAssetId: card.sourceAssetId,
      sourceDigest: card.sourceDigest,
      sourceStartMs,
      sourceEndMs: sourceStartMs + shotDurationMs,
      timelineStartMs,
      role: structure.roles[index]!,
      reason: `${structure.reasons[index]!}；同一原片分段组合，待人工审核内容语义`,
    };
    timelineStartMs += shotDurationMs;
    return shot;
  });
  return EditBlueprintSchema.parse({
    id: `blueprint_${stableDigest({ briefId: brief.id, version: brief.version, direction: structure.direction, shots })}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: brief.version,
    briefId: brief.id,
    editorialDirection: structure.direction,
    direction: structure.direction,
    durationSeconds: timelineStartMs / 1_000,
    shots,
    sourceReuseMode: "same-source-segments",
    capability: OFFLINE_STUDIO_CAPABILITY,
    publishClaimed: false,
    createdAt,
  });
}

function makeBlueprint(
  scope: StudioScope,
  brief: StudioCreativeBrief,
  cards: readonly MaterialCard[],
  structure: (typeof STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES)[number],
  createdAt: string,
  adjustments: readonly OfflineStudioFeedbackAdjustment[] = [],
): EditBlueprint {
  let timelineStartMs = 0;
  const shotAdjustments = offlineShotAdjustments(adjustments, structure, cards);
  const shots = cards.map((card, index) => {
    const adjustment = shotAdjustments[index]!;
    const shot = {
      id: `shot_${stableDigest({ briefId: brief.id, direction: structure.direction, index, cardId: card.id })}`,
      sourceAssetId: card.sourceAssetId,
      sourceDigest: card.sourceDigest,
      sourceStartMs: card.sourceStartMs,
      sourceEndMs: card.sourceEndMs - adjustment.trimMs,
      timelineStartMs,
      role: structure.roles[index]!,
      reason: adjustment.audit.length === 0
        ? structure.reasons[index]!
        : `${structure.reasons[index]!}；${adjustment.audit.join("；")}`,
    };
    timelineStartMs += shot.sourceEndMs - shot.sourceStartMs;
    return shot;
  });
  const durationSeconds = timelineStartMs / 1_000;
  if (durationSeconds < 30 || durationSeconds > 60) throw new Error("renderable-duration-outside-30-60-seconds");
  return EditBlueprintSchema.parse({
    id: `blueprint_${stableDigest({ briefId: brief.id, version: brief.version, direction: structure.direction, shots })}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: brief.version,
    briefId: brief.id,
    editorialDirection: structure.direction,
    direction: structure.direction,
    durationSeconds,
    shots,
    capability: OFFLINE_STUDIO_CAPABILITY,
    publishClaimed: false,
    createdAt,
  });
}

function validateBlueprints(
  values: readonly EditBlueprint[],
  advertisedCapability: EditorialDecisionAdapter["capability"],
  scope: StudioScope,
  brief: StudioCreativeBrief,
  cards: readonly MaterialCard[],
): EditBlueprint[] {
  try {
    if (advertisedCapability.mode === "provider") {
      return validateProviderBlueprints(values, advertisedCapability, scope, brief, cards);
    }
    if (!Array.isArray(values) || values.length !== 2) throw new Error("two-blueprints-required");
    const parsed = values.map((value) => EditBlueprintSchema.parse(value));
    if (new Set(parsed.map((blueprint) => blueprint.id)).size !== 2) throw new Error("duplicate-blueprint-id");
    const cardByAsset = new Map(cards.map((card) => [card.sourceAssetId, card]));
    for (const [blueprintIndex, blueprint] of parsed.entries()) {
      const expectedStructure = STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES[blueprintIndex]!;
      const adjustments = offlineAdjustmentsFromBrief(brief);
      const blueprintCards = blueprint.shots.map((shot) => cardByAsset.get(shot.sourceAssetId));
      if (blueprintCards.some((card) => card === undefined)) {
        throw new Error("blueprint-source-binding-mismatch");
      }
      const shotAdjustments = offlineShotAdjustments(
        adjustments,
        expectedStructure,
        blueprintCards as MaterialCard[],
      );
      if (blueprint.customerId !== scope.customerId || blueprint.sessionId !== scope.sessionId ||
          blueprint.briefId !== brief.id || blueprint.version !== brief.version ||
          !sameCapability(blueprint.capability, advertisedCapability)) {
        throw new Error("blueprint-binding-mismatch");
      }
      if (blueprint.editorialDirection !== expectedStructure.direction ||
          blueprint.direction !== expectedStructure.direction ||
          blueprint.shots.length !== expectedStructure.roles.length ||
          blueprint.shots.some((shot, index) => {
            const adjustment = shotAdjustments[index]!;
            const expectedReason = blueprint.sourceReuseMode === "same-source-segments"
              ? `${expectedStructure.reasons[index]!}；同一原片分段组合，待人工审核内容语义`
              : adjustment.audit.length === 0
                ? expectedStructure.reasons[index]!
                : `${expectedStructure.reasons[index]!}；${adjustment.audit.join("；")}`;
            return shot.role !== expectedStructure.roles[index] || shot.reason !== expectedReason;
          })) {
        throw new Error("blueprint-editorial-structure-mismatch");
      }
      let timelineStartMs = 0;
      for (const shot of blueprint.shots) {
        const card = cardByAsset.get(shot.sourceAssetId);
        if (!card || shot.sourceDigest !== card.sourceDigest ||
            shot.sourceStartMs < card.sourceStartMs || shot.sourceEndMs > card.sourceEndMs ||
            shot.timelineStartMs !== timelineStartMs) {
          throw new Error("blueprint-source-binding-mismatch");
        }
        timelineStartMs += shot.sourceEndMs - shot.sourceStartMs;
      }
      if (Math.abs(blueprint.durationSeconds - timelineStartMs / 1_000) > 0.001) {
        throw new Error("blueprint-duration-mismatch");
      }
    }
    const timelineIdentity = (blueprint: EditBlueprint) => JSON.stringify(
      blueprint.shots.map((shot) => ({
        assetId: shot.sourceAssetId,
        inMs: shot.sourceStartMs,
        outMs: shot.sourceEndMs,
      })),
    );
    const sourceSetIdentity = (blueprint: EditBlueprint) => JSON.stringify(
      blueprint.shots.map((shot) => shot.sourceAssetId).sort(),
    );
    const narrativeRoleIdentity = (blueprint: EditBlueprint) => JSON.stringify(
      blueprint.shots.map((shot) => shot.role),
    );
    const sameSourceSegmentPair = parsed.every((blueprint) =>
      blueprint.sourceReuseMode === "same-source-segments");
    if (parsed[0]!.direction.trim() === parsed[1]!.direction.trim() ||
        (!sameSourceSegmentPair && sourceSetIdentity(parsed[0]!) === sourceSetIdentity(parsed[1]!)) ||
        narrativeRoleIdentity(parsed[0]!) === narrativeRoleIdentity(parsed[1]!) ||
        timelineIdentity(parsed[0]!) === timelineIdentity(parsed[1]!)) {
      throw new Error("blueprint-timelines-not-materially-different");
    }
    return parsed;
  } catch {
    throw new Error("editorial-adapter-contract-mismatch");
  }
}

const REQUIRED_PLATFORM_VARIANTS = Object.freeze([
  "editorial-master",
  "xiaohongshu",
  "douyin",
  "wechat-channels",
] as const);

function validateProviderBlueprints(
  values: readonly EditBlueprint[],
  advertisedCapability: EditorialDecisionAdapter["capability"],
  scope: StudioScope,
  brief: StudioCreativeBrief,
  cards: readonly MaterialCard[],
): EditBlueprint[] {
  if (!Array.isArray(values) || values.length !== REQUIRED_PLATFORM_VARIANTS.length) {
    throw new Error("four-platform-blueprints-required");
  }
  const parsed = values.map((value) => EditBlueprintSchema.parse(value));
  if (new Set(parsed.map((blueprint) => blueprint.id)).size !== parsed.length) {
    throw new Error("duplicate-blueprint-id");
  }
  const variants = parsed.map((blueprint) => blueprint.platformVariant);
  if (variants.some((variant) => variant === undefined) ||
      new Set(variants).size !== REQUIRED_PLATFORM_VARIANTS.length ||
      REQUIRED_PLATFORM_VARIANTS.some((variant) => !variants.includes(variant))) {
    throw new Error("four-platform-blueprints-required");
  }
  const cardByAsset = new Map(cards.map((card) => [card.sourceAssetId, card]));
  for (const blueprint of parsed) {
    if (blueprint.customerId !== scope.customerId || blueprint.sessionId !== scope.sessionId ||
        blueprint.briefId !== brief.id || blueprint.version !== brief.version ||
        !sameCapability(blueprint.capability, advertisedCapability) ||
        blueprint.direction.trim().length === 0 || blueprint.shots.length === 0) {
      throw new Error("blueprint-binding-mismatch");
    }
    let timelineStartMs = 0;
    for (const shot of blueprint.shots) {
      const card = cardByAsset.get(shot.sourceAssetId);
      if (!card || shot.sourceDigest !== card.sourceDigest ||
          shot.sourceStartMs < card.sourceStartMs || shot.sourceEndMs > card.sourceEndMs ||
          shot.timelineStartMs !== timelineStartMs) {
        throw new Error("blueprint-source-binding-mismatch");
      }
      timelineStartMs += shot.sourceEndMs - shot.sourceStartMs;
    }
    if (Math.abs(blueprint.durationSeconds - timelineStartMs / 1_000) > 0.001) {
      throw new Error("blueprint-duration-mismatch");
    }
  }
  const timelineIdentities = parsed.map((blueprint) => JSON.stringify(
    blueprint.shots.map((shot) => ({
      assetId: shot.sourceAssetId,
      inMs: shot.sourceStartMs,
      outMs: shot.sourceEndMs,
    })),
  ));
  if (new Set(parsed.map((blueprint) => blueprint.direction.trim())).size !== parsed.length ||
      new Set(timelineIdentities).size !== parsed.length) {
    throw new Error("blueprint-timelines-not-materially-different");
  }
  return parsed;
}

function offlineAdjustmentsFromBrief(
  brief: StudioCreativeBrief,
): OfflineStudioFeedbackAdjustment[] {
  if (!brief.reviewRequirements.includes(OFFLINE_FEEDBACK_INTERPRETER_REQUIREMENT)) return [];
  const values = brief.reviewRequirements.flatMap((requirement) => {
    if (!requirement.startsWith(OFFLINE_FEEDBACK_RULE_PREFIX)) return [];
    const value = requirement.slice(OFFLINE_FEEDBACK_RULE_PREFIX.length);
    return value === "increase-opening-shot-share" || value === "increase-ending-shot-share" ||
      value === "tighten-pacing"
      ? [value as OfflineStudioFeedbackAdjustment]
      : [];
  });
  return [...new Set(values)];
}

const OFFLINE_FEEDBACK_TARGETS = Object.freeze({
  "increase-opening-shot-share": Object.freeze({
    role: "designer-viewpoint-open",
    expectedPosition: "opening",
    label: "设计师观点开场角色",
  }),
  "increase-ending-shot-share": Object.freeze({
    role: "finished-product-close",
    expectedPosition: "ending",
    label: "成品结尾角色",
  }),
} as const);
const STUDIO_MINIMUM_DURATION_MS = 30_000;
const STUDIO_RENDER_DURATION_GUARD_MS = 250;

function offlineShotAdjustments(
  adjustments: readonly OfflineStudioFeedbackAdjustment[],
  structure: (typeof STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES)[number],
  cards: readonly MaterialCard[],
): Array<{ trimMs: number; audit: string[] }> {
  const shotAdjustments = structure.roles.map(() => ({ trimMs: 0, audit: [] as string[] }));
  const totalDurationMs = cards.reduce(
    (total, card) => total + card.sourceEndMs - card.sourceStartMs,
    0,
  );
  let remainingTrimBudgetMs = Math.max(
    0,
    totalDurationMs - STUDIO_MINIMUM_DURATION_MS - STUDIO_RENDER_DURATION_GUARD_MS,
  );
  for (const adjustment of adjustments) {
    if (adjustment === "tighten-pacing") {
      if (structure.direction !== "chronological-evidence-first") {
        shotAdjustments[0]!.audit.push("不适用（本方向不是离线节奏压缩适用方向），镜头不变");
        continue;
      }
      const nonCoreIndex = structure.roles.findIndex((role) => role === "process-evidence-progress");
      const trimMs = Math.min(STUDIO_RENDER_DURATION_GUARD_MS, remainingTrimBudgetMs);
      if (nonCoreIndex < 0 || trimMs === 0) {
        shotAdjustments[0]!.audit.push("不适用（duration-floor：无安全时长预算可压缩非核心镜头），镜头不变");
        continue;
      }
      shotAdjustments[nonCoreIndex]!.trimMs += trimMs;
      shotAdjustments[nonCoreIndex]!.audit.push(`离线规则：压缩非核心镜头 ${trimMs}ms，加快节奏`);
      remainingTrimBudgetMs -= trimMs;
      continue;
    }
    const target = OFFLINE_FEEDBACK_TARGETS[adjustment];
    const targetIndex = structure.roles.findIndex((role) => role === target.role);
    const positionMatches = target.expectedPosition === "opening"
      ? targetIndex === 0
      : targetIndex === structure.roles.length - 1;
    if (!positionMatches) {
      shotAdjustments[0]!.audit.push(`离线规则：不适用（本方向不含位于${target.expectedPosition === "opening" ? "开场" : "结尾"}的${target.label}），镜头不变`);
      continue;
    }

    const nonTargetIndexes = structure.roles.flatMap((_, index) => index === targetIndex ? [] : [index]);
    const trimBudgetMs = Math.min(remainingTrimBudgetMs, nonTargetIndexes.length * 250);
    if (trimBudgetMs === 0) {
      shotAdjustments[targetIndex]!.audit.push(
        `离线规则：不适用（duration-floor：当前方向已达30秒下限安全边界，无可用缩短预算），${target.label}与其他镜头不变`,
      );
      continue;
    }

    let unallocatedTrimMs = trimBudgetMs;
    for (const index of nonTargetIndexes) {
      const trimMs = Math.min(250, unallocatedTrimMs);
      if (trimMs === 0) break;
      shotAdjustments[index]!.trimMs += trimMs;
      shotAdjustments[index]!.audit.push(
        `离线规则：压缩此非目标镜头${trimMs}ms，提高${target.label}的相对占比`,
      );
      unallocatedTrimMs -= trimMs;
    }
    shotAdjustments[targetIndex]!.audit.push(
      `离线规则：目标${target.label}保持原长，通过压缩其他镜头共${trimBudgetMs}ms提高${target.label}的相对占比`,
    );
    remainingTrimBudgetMs -= trimBudgetMs;
  }
  return shotAdjustments;
}

function latestCardPerAsset(cards: readonly MaterialCard[]): MaterialCard[] {
  const byAsset = new Map<string, MaterialCard>();
  for (const card of cards) {
    const previous = byAsset.get(card.sourceAssetId);
    if (!previous || compareCards(previous, card) < 0) byAsset.set(card.sourceAssetId, card);
  }
  return [...byAsset.values()].sort((left, right) =>
    left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id));
}

function compareCards(left: MaterialCard, right: MaterialCard): number {
  return left.version - right.version ||
    left.createdAt.localeCompare(right.createdAt) ||
    left.id.localeCompare(right.id);
}

function isEightToTwentySecondSegment(card: MaterialCard): boolean {
  const duration = card.sourceEndMs - card.sourceStartMs;
  return duration >= 8_000 && duration <= 20_000;
}

function supportsDefaultDraftDirections(cards: readonly MaterialCard[]): boolean {
  if (cards.length !== 4) return false;
  const durationSeconds = (indexes: readonly number[]) => indexes.reduce((total, index) => {
    const card = cards[index]!;
    return total + card.sourceEndMs - card.sourceStartMs;
  }, 0) / 1_000;
  return [[0, 1, 2], [3, 1, 0]].every((indexes) => {
    const duration = durationSeconds(indexes);
    return duration >= 30 && duration <= 60;
  });
}

function supportsSingleSourceDraftDirections(card: MaterialCard): boolean {
  return card.technicalUsable && card.sourceEndMs - card.sourceStartMs >= 12_000;
}

/**
 * Searches all C(n, 2) possible shared-role pairs without enumerating every
 * group of four. Remaining clips are globally ranked by duration and stable
 * input position; excluding two shared clips means finding the two longest
 * extras inspects at most the first four ranked entries per pair.
 *
 * The search is deterministic O(n log n + n²) time and O(n) space. Every
 * eligible clip is at most 20 seconds, so a three-clip cut cannot exceed 60
 * seconds; if the two longest remaining extras miss the 30-second lower bound,
 * no other extra pair can make that shared pair feasible.
 */
function findDefaultDraftCards(cards: readonly MaterialCard[]): MaterialCard[] | null {
  if (cards.length < 4) return null;
  const durations = cards.map((card) => card.sourceEndMs - card.sourceStartMs);
  const longestFirst = cards.map((_, index) => index)
    .sort((left, right) => durations[right]! - durations[left]! || left - right);
  for (let first = 0; first < cards.length - 1; first += 1) {
    for (let second = first + 1; second < cards.length; second += 1) {
      const extras: number[] = [];
      for (const index of longestFirst) {
        if (index !== first && index !== second) extras.push(index);
        if (extras.length === 2) break;
      }
      if (extras.length < 2) continue;
      const [third, fourth] = extras.sort((left, right) => left - right);
      const selected = [cards[first]!, cards[second]!, cards[third]!, cards[fourth]!];
      if (supportsDefaultDraftDirections(selected)) return selected;
    }
  }
  return null;
}

function requiredEvidenceCardIdGroups(
  demand: ContentDemand,
  assessment: MatchAssessment,
): readonly (readonly string[])[] {
  const requiredRows = assessment.matchedEvidence.slice(1);
  return demand.mustInclude.map((requirement) =>
    requiredRows.find((row) => row.requirement === requirement)?.materialCardIds ?? []);
}

function findDemandBoundDraftCards(
  cards: readonly MaterialCard[],
  requiredEvidence: readonly (readonly string[])[],
): readonly MaterialCard[] | null {
  return searchDemandBoundDraftCards(cards, requiredEvidence).cards;
}

export type DemandBoundDraftSearchResult = Readonly<{
  cards: readonly MaterialCard[] | null;
  diagnostics: Readonly<{
    candidateCount: number;
    requirementCount: number;
    evidenceLinksVisited: number;
    searchOperationsVisited: number;
    budgetExceeded: boolean;
  }>;
}>;

const MAX_DEMAND_BOUND_SEARCH_OPERATIONS = 50_000;

/**
 * Maps required-evidence membership to a BigInt bitmap, rejects impossible
 * coverage before branching, and caps deterministic candidate visits. The
 * cap is deliberately fail-closed: a search that exhausts it returns no
 * cards and reports `budgetExceeded` instead of blocking the scope queue.
 */
export function searchDemandBoundDraftCards(
  cards: readonly MaterialCard[],
  requiredEvidence: readonly (readonly string[])[],
): DemandBoundDraftSearchResult {
  const cardIndexById = new Map(cards.map((card, index) => [card.id, index]));
  const evidenceMasks = cards.map(() => 0n);
  let evidenceLinksVisited = 0;
  for (const [requirementIndex, cardIds] of requiredEvidence.entries()) {
    const requirementBit = 1n << BigInt(requirementIndex);
    for (const cardId of cardIds) {
      evidenceLinksVisited += 1;
      const cardIndex = cardIndexById.get(cardId);
      if (cardIndex !== undefined) evidenceMasks[cardIndex] |= requirementBit;
    }
  }
  const diagnostics = {
    candidateCount: cards.length,
    requirementCount: requiredEvidence.length,
    evidenceLinksVisited,
    searchOperationsVisited: 0,
    budgetExceeded: false,
  };
  if (requiredEvidence.length === 0) return { cards: findDefaultDraftCards(cards), diagnostics };
  if (cards.length < 4) return { cards: null, diagnostics };

  const fullEvidenceMask = (1n << BigInt(requiredEvidence.length)) - 1n;
  const availableEvidenceMask = evidenceMasks.reduce((mask, value) => mask | value, 0n);
  if (availableEvidenceMask !== fullEvidenceMask) return { cards: null, diagnostics };
  const mostRequirementsOnOneCard = evidenceMasks.reduce(
    (maximum, mask) => Math.max(maximum, countSetBits(mask)),
    0,
  );
  if (mostRequirementsOnOneCard * 4 < requiredEvidence.length) {
    return { cards: null, diagnostics };
  }

  const suffixEvidenceMasks = evidenceMasks.map(() => 0n);
  let suffixMask = 0n;
  for (let index = evidenceMasks.length - 1; index >= 0; index -= 1) {
    suffixMask |= evidenceMasks[index]!;
    suffixEvidenceMasks[index] = suffixMask;
  }
  const selectedIndexes: number[] = [];
  let answer: readonly MaterialCard[] | null = null;

  const visitSearchOperation = (): boolean => {
    if (diagnostics.searchOperationsVisited >= MAX_DEMAND_BOUND_SEARCH_OPERATIONS) {
      diagnostics.budgetExceeded = true;
      return false;
    }
    diagnostics.searchOperationsVisited += 1;
    return true;
  };
  const testSelection = () => {
    const selected = selectedIndexes.map((index) => cards[index]!);
    answer = arrangeDefaultDraftCards(selected);
  };
  const fillSelection = (startIndex: number) => {
    if (answer || diagnostics.budgetExceeded) return;
    if (selectedIndexes.length === 4) {
      testSelection();
      return;
    }
    for (let index = startIndex; index < cards.length; index += 1) {
      if (!visitSearchOperation()) return;
      if (selectedIndexes.includes(index)) continue;
      selectedIndexes.push(index);
      fillSelection(index + 1);
      selectedIndexes.pop();
      if (answer || diagnostics.budgetExceeded) return;
    }
  };
  const searchEvidence = (startIndex: number, coveredEvidence: bigint) => {
    if (answer || diagnostics.budgetExceeded) return;
    if (coveredEvidence === fullEvidenceMask) {
      fillSelection(0);
      return;
    }
    if (selectedIndexes.length === 4 || startIndex >= cards.length) return;
    if ((coveredEvidence | suffixEvidenceMasks[startIndex]!) !== fullEvidenceMask) return;
    for (let index = startIndex; index < cards.length; index += 1) {
      if (!visitSearchOperation()) return;
      const nextEvidence = coveredEvidence | evidenceMasks[index]!;
      if (nextEvidence === coveredEvidence) continue;
      selectedIndexes.push(index);
      searchEvidence(index + 1, nextEvidence);
      selectedIndexes.pop();
      if (answer || diagnostics.budgetExceeded) return;
    }
  };

  searchEvidence(0, 0n);
  return { cards: answer, diagnostics };
}

function countSetBits(value: bigint): number {
  let count = 0;
  for (let remaining = value; remaining !== 0n; remaining &= remaining - 1n) count += 1;
  return count;
}

function arrangeDefaultDraftCards(cards: readonly MaterialCard[]): readonly MaterialCard[] | null {
  if (cards.length !== 4) return null;
  for (let first = 0; first < cards.length - 1; first += 1) {
    for (let second = first + 1; second < cards.length; second += 1) {
      const extras = cards.filter((_, index) => index !== first && index !== second);
      const selected = [cards[first]!, cards[second]!, ...extras];
      if (supportsDefaultDraftDirections(selected)) return selected;
    }
  }
  return null;
}

function validateAdapterCard(
  value: MaterialCard,
  advertisedCapability: MediaUnderstandingAdapter["capability"],
  scope: StudioScope,
  sourceAssetId: string,
  sourceDigest: string,
): MaterialCard {
  try {
    const card = MaterialCardSchema.parse(value);
    const capability = StudioCapabilitySchema.parse(advertisedCapability);
    if (
      card.customerId !== scope.customerId ||
      card.sessionId !== scope.sessionId ||
      card.sourceAssetId !== sourceAssetId ||
      card.sourceDigest !== sourceDigest ||
      card.confirmedFacts.length !== 0 ||
      !sameCapability(card.capability, capability)
    ) {
      throw new Error("mismatch");
    }
    return card;
  } catch {
    throw new Error("analysis-adapter-contract-mismatch");
  }
}

function snapshotCapability(
  value: MediaUnderstandingAdapter["capability"],
  error = "analysis-adapter-contract-mismatch",
): MediaUnderstandingAdapter["capability"] {
  try {
    return Object.freeze(structuredClone(StudioCapabilitySchema.parse(value)));
  } catch {
    throw new Error(error);
  }
}

function sameCapability(
  left: MediaUnderstandingAdapter["capability"],
  right: MediaUnderstandingAdapter["capability"],
): boolean {
  return left.mode === right.mode &&
    left.label === right.label &&
    left.version === right.version &&
    left.provider === right.provider;
}

function sameAnalysisBody(left: MaterialCard, right: MaterialCard): boolean {
  const { createdAt: _leftCreatedAt, ...leftBody } = left;
  const { createdAt: _rightCreatedAt, ...rightBody } = right;
  return JSON.stringify(leftBody) === JSON.stringify(rightBody);
}

function sameProviderAnalysisIdentity(left: MaterialCard, right: MaterialCard): boolean {
  const { createdAt: _leftCreatedAt, version: _leftVersion, ...leftBody } = left;
  const { createdAt: _rightCreatedAt, version: _rightVersion, ...rightBody } = right;
  return JSON.stringify(leftBody) === JSON.stringify(rightBody);
}

function capabilityForCards(
  cards: readonly MaterialCard[],
  evidenceByCardId: ReadonlyMap<string, readonly StudioEvidenceRecord[]>,
  degradedReasonsByCardId: ReadonlyMap<string, readonly StudioEvidenceDegradedReason[]>,
  providerIdentitiesByCardId: ReadonlyMap<string, readonly StudioModelProviderIdentity[]>,
  fallback: MediaUnderstandingAdapter["capability"],
): StudioCapability {
  if (cards.length === 0 || cards.some((card) => !evidenceByCardId.has(card.id))) {
    return snapshotCapability(fallback);
  }
  if (cards.some((card) => (degradedReasonsByCardId.get(card.id)?.length ?? 0) > 0)) {
    return OFFLINE_STUDIO_CAPABILITY;
  }
  const identities = cards.flatMap((card) => providerIdentitiesByCardId.get(card.id) ?? []);
  return identities.length > 0 ? studioProviderCapability(identities) : OFFLINE_STUDIO_CAPABILITY;
}

function isProviderEvidencePermissionError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return [
    "not-authorized",
    "asset-not-found",
    "asset-session-mismatch",
    "asset-withdrawn",
    "asset-use-not-allowed",
    "source-digest-mismatch",
  ].includes(error.message);
}

function buildDraftVisualEvidence(
  blueprint: EditBlueprint,
  evidence: readonly StudioEditorialVisualEvidence[],
): StudioDraftVisualEvidence {
  const items = blueprint.shots.flatMap((shot) => {
    const source = evidence.find((candidate) =>
      candidate.provider.layer === "vision"
      && candidate.sourceAssetId === shot.sourceAssetId
      && candidate.sourceDigest === shot.sourceDigest
      && candidate.sourceStartMs <= shot.sourceStartMs
      && candidate.sourceEndMs >= shot.sourceEndMs
      && candidate.receiptJobId.trim().length > 0);
    if (!source) return [];
    const observations = [...new Set(source.observations.map((value) => value.trim()).filter(Boolean))].slice(0, 64);
    if (observations.length === 0) return [];
    return [{
      shotId: shot.id,
      sourceAssetId: shot.sourceAssetId,
      sourceDigest: shot.sourceDigest,
      sourceStartMs: shot.sourceStartMs,
      sourceEndMs: shot.sourceEndMs,
      receiptJobId: source.receiptJobId,
      provider: source.provider,
      observations,
      selectionReason: shot.reason,
    }];
  });
  if (items.length === blueprint.shots.length) {
    return { status: "used", items, degradedReason: null };
  }
  if (items.length > 0) {
    return {
      status: "partial",
      items,
      degradedReason: `${blueprint.shots.length - items.length} 个镜头没有匹配到真实视觉模型回执`,
    };
  }
  return {
    status: "not-used",
    items: [],
    degradedReason: "本版未取得本地、非合成且与素材摘要一致的视觉模型回执",
  };
}

function assertConfirmBriefInput(input: ConfirmBriefInput): void {
  if (!input || !Array.isArray(input.selectedAssetIds)) throw new Error("selected-asset-ids-required");
  if (!Array.isArray(input.confirmedFactIds)) throw new Error("confirmed-fact-ids-required");
  if (!Array.isArray(input.reviewRequirements)) throw new Error("review-requirements-required");
  assertText(input.opportunityId, "opportunity-id-required");
  assertText(input.matchAssessmentId, "match-assessment-id-required");
  assertText(input.operationKey, "brief-operation-key-required");
}

function assertCreateDemandInput(input: CreateDemandInput): void {
  if (!input || !Array.isArray(input.mustInclude) || !Array.isArray(input.mustAvoid)) {
    throw new Error("demand-arrays-required");
  }
  assertText(input.goal, "demand-goal-required");
  assertText(input.targetAudience, "demand-target-audience-required");
  assertText(input.desiredEffect, "demand-desired-effect-required");
  assertText(input.operationKey, "demand-operation-key-required");
}

function assertDemandLimits(input: Readonly<{
  goal: string;
  targetAudience: string;
  desiredEffect: string;
  mustInclude: readonly string[];
  mustAvoid: readonly string[];
}>): void {
  assertTextLimit(input.goal, STUDIO_INPUT_LIMITS.goal, "demand-goal-too-long");
  assertTextLimit(input.targetAudience, STUDIO_INPUT_LIMITS.targetAudience, "demand-target-audience-too-long");
  assertTextLimit(input.desiredEffect, STUDIO_INPUT_LIMITS.desiredEffect, "demand-desired-effect-too-long");
  assertStructuredValueLimits(input.mustInclude, "demand-must-include");
  assertStructuredValueLimits(input.mustAvoid, "demand-must-avoid");
}

function assertCustomerFeedbackLimits(input: Readonly<{
  comment: string | null;
  mustKeep: readonly string[];
  mustRemove: readonly string[];
  needsReshoot: readonly string[];
}>): void {
  if (input.comment !== null) {
    assertTextLimit(input.comment, STUDIO_INPUT_LIMITS.feedbackComment, "customer-feedback-comment-too-long");
  }
  assertStructuredValueLimits(input.mustKeep, "customer-feedback-must-keep");
  assertStructuredValueLimits(input.mustRemove, "customer-feedback-must-remove");
  assertStructuredValueLimits(input.needsReshoot, "customer-feedback-needs-reshoot");
}

function assertStructuredValueLimits(values: readonly string[], codePrefix: string): void {
  if (values.length > STUDIO_INPUT_LIMITS.structuredItems) throw new Error(`${codePrefix}-too-many`);
  for (const value of values) {
    assertTextLimit(value, STUDIO_INPUT_LIMITS.structuredItem, `${codePrefix}-item-too-long`);
  }
}

function assertTextLimit(value: string, maximum: number, code: string): void {
  if (value.length > maximum) throw new Error(code);
}

function normalizeOperationKey(value: string, tooLongCode: string): string {
  const normalized = normalizeDemandText(value);
  if (normalized.length > STUDIO_INPUT_LIMITS.operationKey) throw new Error(tooLongCode);
  return normalized;
}

function normalizeDemandValues(values: readonly string[], error: string): string[] {
  return values.map((value) => {
    assertText(value, error);
    const normalized = normalizeDemandText(value);
    if (!normalized) throw new Error(error);
    return normalized;
  });
}

function assertFeedbackInput(input: RecordFeedbackInput): void {
  if (!input || !["adopt", "reject", "request-changes"].includes(input.action)) {
    throw new Error("feedback-action-not-allowed");
  }
  assertText(input.draftId, "draft-id-required");
  assertText(input.operationKey, "feedback-operation-key-required");
  if (input.reason !== null && (typeof input.reason !== "string" || !input.reason.trim())) {
    throw new Error("feedback-reason-invalid");
  }
  if (input.action === "request-changes" && input.reason === null) {
    throw new Error("feedback-reason-required");
  }
  if (input.action === "adopt" && input.reason !== null) {
    throw new Error("adopt-feedback-reason-must-be-null");
  }
}

function assertDistributionEventInput(input: RecordDistributionEventInput): void {
  if (!input || !["download-started", "system-share-invoked", "share-fallback-download"].includes(input.action)) {
    throw new Error("distribution-action-not-allowed");
  }
  assertText(input.draftId, "draft-id-required");
  assertText(input.operationKey, "distribution-operation-key-required");
}

function assertRecordCustomerFeedbackInput(input: RecordCustomerFeedbackInput): void {
  if (!input || !Array.isArray(input.issueCategories) || !Array.isArray(input.mustKeep) ||
      !Array.isArray(input.mustRemove) || !Array.isArray(input.needsReshoot)) {
    throw new Error("customer-feedback-arrays-required");
  }
  assertText(input.draftId, "draft-id-required");
  assertText(input.operationKey, "customer-feedback-operation-key-required");
  if (!["satisfied", "partially-satisfied", "unsatisfied"].includes(input.satisfaction)) {
    throw new Error("customer-feedback-satisfaction-invalid");
  }
  if (!["customer-self-declared", "operator-on-behalf"].includes(input.submittedBy)) {
    throw new Error("customer-feedback-submitter-invalid");
  }
  if (input.comment !== null && typeof input.comment !== "string") {
    throw new Error("customer-feedback-comment-invalid");
  }
}

function normalizeStructuredValues(values: readonly string[], error: string): string[] {
  const normalized = values.map((value) => {
    assertText(value, error);
    return normalizeDemandText(value);
  });
  if (new Set(normalized).size !== normalized.length) throw new Error("duplicate-value");
  return normalized;
}

function normalizeUnique(values: readonly string[], emptyError: string): string[] {
  const normalized = values.map((value) => {
    assertText(value, emptyError);
    return value.trim();
  });
  if (new Set(normalized).size !== normalized.length) throw new Error("duplicate-value");
  return normalized;
}

function sortVersioned<T extends { version: number; id: string; createdAt?: string }>(
  values: readonly T[],
): T[] {
  return [...values].sort((left, right) =>
    left.version - right.version ||
    (left.createdAt ?? "").localeCompare(right.createdAt ?? "") ||
    left.id.localeCompare(right.id));
}

function assertScope(scope: StudioScope): void {
  assertText(scope?.actorId, "actor-id-required");
  assertText(scope?.customerId, "customer-id-required");
  assertText(scope?.sessionId, "session-id-required");
}

function assertText(value: string, error: string): void {
  if (typeof value !== "string" || !value.trim()) throw new Error(error);
}

function scopedIdentity(scope: StudioScope) {
  return { customerId: scope.customerId, sessionId: scope.sessionId };
}

function cardIdentity(cards: readonly MaterialCard[]) {
  return cards.map((card) => ({ id: card.id, version: card.version, digest: card.sourceDigest }));
}

function stableDigest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function isRecordValue(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stableBytesDigest(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function compareCodePoints(left: string, right: string): number {
  const leftScalars = Array.from(left);
  const rightScalars = Array.from(right);
  const length = Math.min(leftScalars.length, rightScalars.length);
  for (let index = 0; index < length; index += 1) {
    const leftValue = leftScalars[index]!.codePointAt(0)!;
    const rightValue = rightScalars[index]!.codePointAt(0)!;
    if (leftValue !== rightValue) return leftValue < rightValue ? -1 : 1;
  }
  return leftScalars.length - rightScalars.length;
}

function isWithdrawalError(error: unknown): boolean {
  return error instanceof Error && [
    "asset-withdrawn",
    "asset-not-found",
    "asset-view-not-allowed",
    "asset-use-not-allowed",
  ].some((code) => error.message.includes(code));
}

function clone<T>(value: T): T {
  return structuredClone(value);
}
