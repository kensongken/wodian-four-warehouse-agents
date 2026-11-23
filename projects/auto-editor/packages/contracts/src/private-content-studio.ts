import { z } from "zod";

import { StudioAnalysisLayerSchema, StudioModelProviderIdentitySchema } from "./studio-model-analysis";

const NonEmptyIdSchema = z.string().trim().min(1);
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const BlobIdSchema = z.string().regex(/^blob_[a-f0-9]{64}$/);
const PositiveVersionSchema = z.number().int().positive();
const DateTimeSchema = z.string().datetime();

export const STUDIO_INPUT_LIMITS = Object.freeze({
  goal: 500,
  targetAudience: 200,
  desiredEffect: 500,
  feedbackComment: 1000,
  structuredItem: 200,
  structuredItems: 20,
  operationKey: 200,
} as const);
export const StudioOperationKeySchema = z.string().trim().min(1).max(STUDIO_INPUT_LIMITS.operationKey);
const StudioGoalSchema = z.string().trim().min(1).max(STUDIO_INPUT_LIMITS.goal);
const StudioTargetAudienceSchema = z.string().trim().min(1).max(STUDIO_INPUT_LIMITS.targetAudience);
const StudioDesiredEffectSchema = z.string().trim().min(1).max(STUDIO_INPUT_LIMITS.desiredEffect);
const StudioStructuredItemSchema = z.string().trim().min(1).max(STUDIO_INPUT_LIMITS.structuredItem);
const StudioStructuredItemsSchema = z.array(StudioStructuredItemSchema).max(STUDIO_INPUT_LIMITS.structuredItems);
const SafeRelativePathSchema = z.string().trim().min(1).max(512).refine((value) => {
  if (value.startsWith("/") || value.startsWith("\\") || value.includes("\\") || /^[A-Za-z]:/.test(value)) return false;
  const segments = value.split("/");
  return segments.every((segment) => segment.length > 0 && segment !== "." && segment !== "..");
}, "path-must-be-safe-relative-path");

const unique = (values: readonly string[]) => new Set(values).size === values.length;

export const StudioContentTypeSchema = z.enum([
  "jewelry-making-process",
  "single-product",
  "designer-viewpoint",
]);

export const StudioPermissionStateSchema = z.enum([
  "allowed",
  "review-required",
  "blocked",
  "withdrawn",
]);

const StudioCaptureContextSchema = z.object({
  envelopeId: NonEmptyIdSchema,
  adapterName: NonEmptyIdSchema,
  adapterVersion: NonEmptyIdSchema,
}).strict();

export const StudioCapabilitySchema = z.object({
  mode: z.enum(["offline-fallback", "provider"]),
  label: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  provider: NonEmptyIdSchema.nullable(),
}).strict().superRefine((value, ctx) => {
  if (value.mode === "offline-fallback" && value.label !== "离线/模拟分析") {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["label"],
      message: "offline-fallback-label-required",
    });
  }
  if (value.mode === "offline-fallback" && value.provider !== null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["provider"],
      message: "offline-fallback-provider-must-be-null",
    });
  }
  if (value.mode === "provider" && value.provider === null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["provider"],
      message: "provider-name-required",
    });
  }
});

export const StudioMediaAssetSchema = z.object({
  id: NonEmptyIdSchema,
  importBindingId: NonEmptyIdSchema,
  blobId: BlobIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema.nullable().default(null),
  version: PositiveVersionSchema,
  digest: DigestSchema,
  blobDigest: DigestSchema,
  mediaKind: z.enum(["video", "image", "audio"]),
  byteLength: z.number().int().positive(),
  fileName: NonEmptyIdSchema,
  capturedAt: DateTimeSchema.nullable(),
  importedAt: DateTimeSchema,
  viewPermission: StudioPermissionStateSchema,
  usePermission: StudioPermissionStateSchema,
  captureContext: StudioCaptureContextSchema.nullable().default(null),
}).strict().superRefine((value, ctx) => {
  if (value.importBindingId !== value.id) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["importBindingId"],
      message: "asset-import-binding-id-mismatch",
    });
  }
  if (value.blobDigest !== value.digest) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["blobDigest"],
      message: "asset-blob-digest-mismatch",
    });
  }
});

const SourceIntervalShape = {
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
} as const;

export const MaterialCardSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
  contentType: StudioContentTypeSchema,
  technicalUsable: z.boolean(),
  technicalFlags: z.array(NonEmptyIdSchema),
  observations: z.array(NonEmptyIdSchema),
  confirmedFacts: z.array(NonEmptyIdSchema),
  pendingFacts: z.array(NonEmptyIdSchema),
  capability: StudioCapabilitySchema,
  createdAt: DateTimeSchema,
}).strict().refine((value) => value.sourceEndMs > value.sourceStartMs, {
  message: "source-end-must-follow-start",
  path: ["sourceEndMs"],
});

const InventoryItemSchema = z.object({
  materialCardId: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  selectionReason: NonEmptyIdSchema,
}).strict();

export const MaterialInventorySchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  asOf: DateTimeSchema,
  items: z.array(InventoryItemSchema),
  capability: StudioCapabilitySchema,
}).strict().superRefine((value, ctx) => {
  if (!unique(value.items.map((item) => item.sourceAssetId))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["items"], message: "duplicate-source-asset-id" });
  }
  if (!unique(value.items.map((item) => item.materialCardId))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["items"], message: "duplicate-material-card-id" });
  }
});

export const ContentDemandSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  contentType: StudioContentTypeSchema,
  goal: StudioGoalSchema,
  targetAudience: StudioTargetAudienceSchema,
  desiredEffect: StudioDesiredEffectSchema,
  mustInclude: StudioStructuredItemsSchema,
  mustAvoid: StudioStructuredItemsSchema,
  submittedBy: z.enum(["customer-self-declared", "operator-on-behalf"]),
  status: z.enum(["current", "superseded"]),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (!unique(value.mustInclude)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["mustInclude"], message: "duplicate-must-include" });
  }
  if (!unique(value.mustAvoid)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["mustAvoid"], message: "duplicate-must-avoid" });
  }
});

export const StudioEvidenceKindSchema = z.enum([
  "permission",
  "human-confirmed-fact",
  "verified-technical-fact",
  "model-suggestion",
]);

export const StudioEvidenceRecordSchema = z.object({
  materialCardId: NonEmptyIdSchema,
  kind: StudioEvidenceKindSchema,
  value: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
  receiptJobId: NonEmptyIdSchema.nullable(),
  layer: StudioAnalysisLayerSchema.nullable(),
  reason: NonEmptyIdSchema,
}).strict().superRefine((value, ctx) => {
  if (value.sourceEndMs <= value.sourceStartMs) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceEndMs"], message: "source-end-must-follow-start" });
  }
  const noReceipt = value.receiptJobId === null && value.layer === null;
  const technicalReceipt = value.receiptJobId !== null &&
    (value.layer === "technical" || value.layer === "scene");
  const modelReceipt = value.receiptJobId !== null && value.layer !== null;
  if ((value.kind === "permission" || value.kind === "human-confirmed-fact") && !noReceipt ||
      value.kind === "verified-technical-fact" && !technicalReceipt ||
      value.kind === "model-suggestion" && !modelReceipt) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["kind"], message: "studio-evidence-record-kind-mismatch" });
  }
});

export const StudioEvidenceDegradedReasonCodeSchema = z.enum([
  "studio-evidence-required-provider-missing",
  "studio-evidence-required-provider-offline",
  "studio-evidence-required-provider-synthetic",
]);

export const StudioEvidenceDegradedReasonSchema = z.object({
  code: StudioEvidenceDegradedReasonCodeSchema,
  layer: StudioAnalysisLayerSchema,
  message: NonEmptyIdSchema,
}).strict().superRefine((value, ctx) => {
  const expected = value.code === "studio-evidence-required-provider-missing"
    ? "Required " + value.layer + " provider receipt is missing"
    : value.code === "studio-evidence-required-provider-offline"
      ? "Required " + value.layer + " receipt uses offline-fallback"
      : "Required " + value.layer + " receipt is synthetic";
  if (value.message !== expected) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["message"], message: "studio-evidence-degraded-message-mismatch" });
  }
});

export const MatchEvidenceSourceSchema = z.object({
  materialCardId: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
  evidenceKind: StudioEvidenceKindSchema,
  reason: NonEmptyIdSchema,
}).strict().refine((value) => value.sourceEndMs > value.sourceStartMs, {
  message: "source-end-must-follow-start",
  path: ["sourceEndMs"],
});

const MatchEvidenceSchema = z.object({
  requirement: NonEmptyIdSchema,
  materialCardIds: z.array(NonEmptyIdSchema).min(1).refine(unique, "duplicate-material-card-id"),
  explanation: NonEmptyIdSchema,
  sources: z.array(MatchEvidenceSourceSchema).min(1).optional(),
}).strict().superRefine((value, ctx) => {
  if (value.sources?.some((source) => !value.materialCardIds.includes(source.materialCardId))) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["sources"],
      message: "match-evidence-source-card-mismatch",
    });
  }
});

export const MatchAssessmentSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  demandId: NonEmptyIdSchema,
  inventoryId: NonEmptyIdSchema,
  verdict: z.enum(["ready", "partial", "blocked"]),
  matchedEvidence: z.array(MatchEvidenceSchema),
  unmetNeeds: z.array(NonEmptyIdSchema),
  recommendedCaptures: z.array(NonEmptyIdSchema),
  safeScope: NonEmptyIdSchema,
  capability: StudioCapabilitySchema,
  createdAt: DateTimeSchema,
}).strict();

export const ContentOpportunitySchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  demandId: NonEmptyIdSchema,
  matchAssessmentId: NonEmptyIdSchema,
  contentType: StudioContentTypeSchema,
  goal: NonEmptyIdSchema,
  materialCardIds: z.array(NonEmptyIdSchema).min(1),
  rationale: NonEmptyIdSchema,
  capability: StudioCapabilitySchema,
  createdAt: DateTimeSchema,
}).strict().refine((value) => unique(value.materialCardIds), {
  message: "duplicate-material-card-id",
  path: ["materialCardIds"],
});

export const StudioCreativeBriefSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  demandId: NonEmptyIdSchema,
  matchAssessmentId: NonEmptyIdSchema,
  parentBriefId: NonEmptyIdSchema.nullable(),
  feedbackId: NonEmptyIdSchema.nullable(),
  revisionProposalId: NonEmptyIdSchema.nullable(),
  opportunityId: NonEmptyIdSchema,
  contentType: StudioContentTypeSchema,
  goal: NonEmptyIdSchema,
  selectedAssetIds: z.array(NonEmptyIdSchema).min(1),
  confirmedFactIds: z.array(NonEmptyIdSchema),
  reviewRequirements: z.array(NonEmptyIdSchema).min(1),
  status: z.literal("confirmed"),
  capability: StudioCapabilitySchema,
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (!unique(value.selectedAssetIds)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["selectedAssetIds"], message: "duplicate-source-asset-id" });
  }
  if (!unique(value.confirmedFactIds)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["confirmedFactIds"], message: "duplicate-confirmed-fact-id" });
  }
});

const GapSchema = z.object({
  id: NonEmptyIdSchema,
  missingEvidence: NonEmptyIdSchema,
  impactWithoutCapture: NonEmptyIdSchema,
  recommendedCapture: NonEmptyIdSchema,
}).strict();

export const GapListSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  contentType: StudioContentTypeSchema,
  goal: NonEmptyIdSchema,
  gaps: z.array(GapSchema).min(1),
  capability: StudioCapabilitySchema,
  createdAt: DateTimeSchema,
}).strict();

export const StudioEditorialDirectionSchema = z.enum([
  "chronological-evidence-first",
  "designer-viewpoint-first",
]);

export const StudioBlueprintShotRoleSchema = z.enum([
  "process-evidence-open",
  "process-evidence-progress",
  "finished-product-close",
  "designer-viewpoint-open",
  "design-detail-explanation",
  "process-evidence-return",
]);

const BlueprintShotSchema = z.object({
  ...SourceIntervalShape,
  id: NonEmptyIdSchema,
  timelineStartMs: z.number().int().nonnegative(),
  role: StudioBlueprintShotRoleSchema.optional(),
  reason: NonEmptyIdSchema,
}).strict().refine((value) => value.sourceEndMs > value.sourceStartMs, {
  message: "source-end-must-follow-start",
  path: ["sourceEndMs"],
});

export const StudioCaptionCueSchema = z.object({
  id: NonEmptyIdSchema,
  ...SourceIntervalShape,
  timelineStartMs: z.number().int().nonnegative(),
  timelineEndMs: z.number().int().positive(),
  text: z.string().trim().min(1).max(500),
  reviewStatus: z.enum(["model-suggestion", "human-approved"]),
}).strict().superRefine((value, ctx) => {
  if (value.sourceEndMs <= value.sourceStartMs) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceEndMs"], message: "source-end-must-follow-start" });
  }
  if (value.timelineEndMs <= value.timelineStartMs) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["timelineEndMs"], message: "caption-end-must-follow-start" });
  }
});

export const StudioCaptionTemplateSchema = z.enum([
  "clean-outline",
  "social-highlight",
  "classic-bottom",
]);

export const StudioCaptionStyleSchema = z.object({
  template: StudioCaptionTemplateSchema,
  fontSizePx: z.number().int().min(36).max(64),
  primaryColor: z.string().regex(/^#[0-9A-F]{6}$/),
  outlineColor: z.string().regex(/^#[0-9A-F]{6}$/),
  outlineWidthPx: z.number().int().min(2).max(6),
  bottomMarginPx: z.number().int().min(100).max(280),
  maxCharsPerLine: z.number().int().min(8).max(18),
}).strict();

export const StudioTransitionPlanSchema = z.discriminatedUnion("style", [
  z.object({ style: z.literal("cut"), durationMs: z.literal(0) }).strict(),
  z.object({
    style: z.literal("fade-through-black"),
    durationMs: z.number().int().min(80).max(500),
  }).strict(),
]);

export const StudioBeatSyncPlanSchema = z.object({
  mode: z.enum(["beat-synced", "rule-fallback"]),
  detector: z.literal("audio-energy-onset-v1"),
  detectedBeatCount: z.number().int().nonnegative(),
  alignedCutCount: z.number().int().nonnegative(),
  cutTimesMs: z.array(z.number().int().positive()).max(64),
  toleranceMs: z.number().int().min(80).max(600),
  confidence: z.number().min(0).max(1),
  reason: NonEmptyIdSchema,
}).strict().superRefine((value, ctx) => {
  if (value.alignedCutCount !== value.cutTimesMs.length) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["cutTimesMs"], message: "beat-cut-count-mismatch" });
  }
  if (value.alignedCutCount > value.detectedBeatCount) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["alignedCutCount"], message: "aligned-cuts-exceed-detected-beats" });
  }
  if ((value.mode === "beat-synced") !== (value.alignedCutCount > 0)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["mode"], message: "beat-sync-mode-mismatch" });
  }
});

export const StudioMusicRightsSchema = z.object({
  sourceKind: z.enum(["customer-owned", "commercial-license", "public-domain", "local-generated-test"]),
  licenseNote: z.string().trim().min(1).max(500),
  previewUseAllowed: z.literal(true),
  publishUseAllowed: z.boolean(),
  confirmedBy: NonEmptyIdSchema,
  confirmedAt: DateTimeSchema,
}).strict();

export const StudioMusicMixPlanSchema = z.object({
  mode: z.literal("mixed"),
  trackAssetId: NonEmptyIdSchema,
  trackDigest: DigestSchema,
  trackFileName: z.string().trim().min(1).max(255),
  trackStartMs: z.number().int().nonnegative().max(3_600_000),
  originalGainDb: z.number().min(-24).max(6),
  musicGainDb: z.number().min(-36).max(0),
  ducking: z.object({
    enabled: z.literal(true),
    threshold: z.number().min(0.001).max(1),
    ratio: z.number().min(1).max(20),
    attackMs: z.number().int().min(1).max(500),
    releaseMs: z.number().int().min(20).max(2_000),
  }).strict(),
  loopToFit: z.boolean(),
  rights: StudioMusicRightsSchema,
  reason: NonEmptyIdSchema,
}).strict();

export const EditBlueprintSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  briefId: NonEmptyIdSchema,
  platformVariant: z.enum([
    "editorial-master",
    "xiaohongshu",
    "douyin",
    "wechat-channels",
  ]).optional(),
  editorialDirection: StudioEditorialDirectionSchema.optional(),
  direction: NonEmptyIdSchema,
  durationSeconds: z.number().min(6).max(60),
  shots: z.array(BlueprintShotSchema).min(1),
  captionCues: z.array(StudioCaptionCueSchema).max(500).optional(),
  captionStyle: StudioCaptionStyleSchema.optional(),
  transitionPlan: StudioTransitionPlanSchema.optional(),
  beatSyncPlan: StudioBeatSyncPlanSchema.optional(),
  musicMixPlan: StudioMusicMixPlanSchema.optional(),
  sourceReuseMode: z.literal("same-source-segments").optional(),
  capability: StudioCapabilitySchema,
  publishClaimed: z.literal(false),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (!unique(value.shots.map((shot) => shot.id))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["shots"], message: "duplicate-shot-id" });
  }
  if (!unique(value.shots.map((shot) => shot.sourceAssetId)) && value.sourceReuseMode !== "same-source-segments") {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["shots"], message: "duplicate-source-asset-id" });
  }
  if (value.sourceReuseMode === "same-source-segments" &&
      !unique(value.shots.map((shot) => `${shot.sourceAssetId}:${shot.sourceStartMs}:${shot.sourceEndMs}`))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["shots"], message: "duplicate-source-interval" });
  }
  for (const [index, cue] of (value.captionCues ?? []).entries()) {
    const shot = value.shots.find((candidate) =>
      candidate.sourceAssetId === cue.sourceAssetId
      && candidate.sourceDigest === cue.sourceDigest
      && candidate.sourceStartMs <= cue.sourceStartMs
      && candidate.sourceEndMs >= cue.sourceEndMs
      && candidate.timelineStartMs + cue.sourceStartMs - candidate.sourceStartMs === cue.timelineStartMs
      && candidate.timelineStartMs + cue.sourceEndMs - candidate.sourceStartMs === cue.timelineEndMs);
    if (!shot || cue.timelineEndMs > value.durationSeconds * 1_000 + 1) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["captionCues", index], message: "caption-cue-shot-binding-mismatch" });
    }
  }
  if (((value.captionCues?.length ?? 0) > 0) !== (value.captionStyle !== undefined)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["captionStyle"], message: "caption-style-and-cues-must-match" });
  }
});

const DraftSourceMapEntrySchema = z.object({
  ...SourceIntervalShape,
  shotId: NonEmptyIdSchema,
  reason: NonEmptyIdSchema,
}).strict().refine((value) => value.sourceEndMs > value.sourceStartMs, {
  message: "source-end-must-follow-start",
  path: ["sourceEndMs"],
});

export const ExternalEditHandoffStatusSchema = z.enum([
  "preparing",
  "ready",
  "editing",
  "candidate-found",
  "returned",
  "degraded",
  "failed",
  "cancelled",
]);

export const ExternalEditHandoffModeSchema = z.enum(["jianying-draft", "file-package"]);

export const StudioPlatformVariantSchema = z.enum([
  "editorial-master",
  "xiaohongshu",
  "douyin",
  "wechat-channels",
]);

export const ExternalEditSourceAssetBindingSchema = z.object({
  sourceAssetId: NonEmptyIdSchema,
  digest: DigestSchema,
}).strict();

export const ExternalEditHandoffSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  parentDraftId: NonEmptyIdSchema,
  parentDraftVersion: PositiveVersionSchema,
  parentDraftDigest: DigestSchema,
  platformVariant: StudioPlatformVariantSchema,
  revisionInstruction: z.string().trim().min(1).max(2_000),
  mustKeep: StudioStructuredItemsSchema.refine(unique, "duplicate-must-keep"),
  includeSources: z.boolean(),
  sourceAssetBindings: z.array(ExternalEditSourceAssetBindingSchema).max(100),
  mode: ExternalEditHandoffModeSchema,
  adapterId: NonEmptyIdSchema.nullable(),
  adapterVersion: NonEmptyIdSchema.nullable(),
  status: ExternalEditHandoffStatusSchema,
  operationKey: StudioOperationKeySchema,
  packageDigest: DigestSchema.nullable(),
  publishClaimed: z.literal(false),
  createdAt: DateTimeSchema,
  updatedAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (!unique(value.sourceAssetBindings.map((item) => item.sourceAssetId))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceAssetBindings"], message: "duplicate-source-asset-id" });
  }
  if (value.includeSources !== (value.sourceAssetBindings.length > 0)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceAssetBindings"], message: "source-consent-binding-mismatch" });
  }
  if (value.mode === "jianying-draft" && (!value.adapterId || !value.adapterVersion)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["adapterId"], message: "verified-adapter-required" });
  }
});

const ExternalEditPackageFileSchema = z.object({
  relativePath: SafeRelativePathSchema,
  role: z.enum(["parent-video", "source-video", "subtitle", "timeline", "instructions"]),
  digest: DigestSchema,
  sizeBytes: z.number().int().positive(),
}).strict();

export const ExternalEditPackageSchema = z.object({
  id: NonEmptyIdSchema,
  handoffId: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  parentDraftId: NonEmptyIdSchema,
  packageDigest: DigestSchema,
  files: z.array(ExternalEditPackageFileSchema).min(1).max(200),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (!unique(value.files.map((file) => file.relativePath))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["files"], message: "duplicate-package-path" });
  }
});

export const ExternalEditReturnCandidateSchema = z.object({
  id: NonEmptyIdSchema,
  handoffId: NonEmptyIdSchema,
  parentDraftId: NonEmptyIdSchema,
  relativePath: SafeRelativePathSchema,
  digest: DigestSchema,
  sizeBytes: z.number().int().positive(),
  durationSeconds: z.number().positive().max(3_600),
  mediaFacts: z.object({
    width: z.number().int().positive().max(16_384),
    height: z.number().int().positive().max(16_384),
  }).strict(),
  stableSince: DateTimeSchema,
  detectedAt: DateTimeSchema,
  status: z.enum(["pending-review", "accepted", "ignored", "invalid"]),
}).strict();

export const ExternalEditReturnedDraftBindingSchema = z.object({
  origin: z.literal("external-editor-return"),
  // A file appearing in the dedicated return folder proves the return path,
  // not which desktop editor produced its bytes. Keep the old literal for
  // durable-state compatibility, while all new returns use the explicit
  // unverified value below.
  externalEditor: z.enum(["jianying-desktop", "unverified-external-file"]),
  declaredEditor: z.literal("jianying-desktop").optional(),
  editorProvenance: z.literal("return-folder-only").optional(),
  parentDraftId: NonEmptyIdSchema,
  handoffId: NonEmptyIdSchema,
  lineageConfidence: z.literal("parent-only"),
}).strict().superRefine((value, ctx) => {
  if (value.externalEditor === "unverified-external-file"
      && (value.declaredEditor !== "jianying-desktop" || value.editorProvenance !== "return-folder-only")) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["editorProvenance"],
      message: "external-editor-provenance-required",
    });
  }
});

export const StudioDraftQualityCheckSchema = z.object({
  id: z.enum([
    "container-format",
    "black-frames",
    "frozen-video",
    "long-silence",
    "audio-level",
    "caption-safe-area",
  ]),
  status: z.enum(["passed", "needs-review", "failed"]),
  measuredValue: z.number().finite().nullable(),
  threshold: NonEmptyIdSchema,
  message: NonEmptyIdSchema,
}).strict();

export const StudioDraftQualityReportSchema = z.object({
  version: z.literal(1),
  status: z.enum(["passed", "needs-review", "failed"]),
  checks: z.array(StudioDraftQualityCheckSchema).length(6),
}).strict().superRefine((value, ctx) => {
  if (!unique(value.checks.map((check) => check.id))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["checks"], message: "duplicate-quality-check-id" });
  }
  const expected = value.checks.some((check) => check.status === "failed")
    ? "failed"
    : value.checks.some((check) => check.status === "needs-review")
      ? "needs-review"
      : "passed";
  if (value.status !== expected) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["status"], message: "quality-report-status-mismatch" });
  }
});

export const StudioCaptionRevisionBindingSchema = z.object({
  parentDraftId: NonEmptyIdSchema,
  changedCueIds: z.array(NonEmptyIdSchema),
  styleChanged: z.boolean(),
}).strict().refine((value) => unique(value.changedCueIds), {
  message: "duplicate-caption-revision-cue-id",
  path: ["changedCueIds"],
});

export const StudioApprovedCaptionImportBindingSchema = z.object({
  sourceDigest: DigestSchema,
  subtitleDigest: DigestSchema,
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
  approvedAt: DateTimeSchema,
  approvalScope: z.literal("local-human-approved-candidate"),
  productionApproved: z.literal(false),
}).strict().refine((value) => value.sourceEndMs > value.sourceStartMs, {
  message: "source-end-must-follow-start",
  path: ["sourceEndMs"],
});

export const StudioShareOptimizationBindingSchema = z.object({
  parentDraftId: NonEmptyIdSchema,
  targetWidth: z.literal(720),
  targetHeight: z.literal(1280),
  mode: z.literal("fit-pad-no-crop"),
}).strict();

export const StudioDraftVisualEvidenceItemSchema = z.object({
  shotId: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
  receiptJobId: NonEmptyIdSchema,
  provider: StudioModelProviderIdentitySchema.refine((value) => value.layer === "vision", {
    message: "draft-visual-evidence-provider-layer-mismatch",
  }),
  observations: z.array(NonEmptyIdSchema).min(1).max(64),
  selectionReason: NonEmptyIdSchema,
}).strict().refine((value) => value.sourceEndMs > value.sourceStartMs, {
  message: "source-end-must-follow-start",
  path: ["sourceEndMs"],
});

export const StudioDraftVisualEvidenceSchema = z.object({
  status: z.enum(["used", "partial", "not-used"]),
  items: z.array(StudioDraftVisualEvidenceItemSchema).max(256),
  degradedReason: NonEmptyIdSchema.nullable(),
}).strict().superRefine((value, ctx) => {
  if (value.status === "used" && (value.items.length === 0 || value.degradedReason !== null)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["status"], message: "used-visual-evidence-state-invalid" });
  }
  if (value.status === "partial" && (value.items.length === 0 || value.degradedReason === null)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["status"], message: "partial-visual-evidence-state-invalid" });
  }
  if (value.status === "not-used" && (value.items.length !== 0 || value.degradedReason === null)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["status"], message: "unused-visual-evidence-state-invalid" });
  }
  if (!unique(value.items.map((item) => item.shotId))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["items"], message: "duplicate-visual-evidence-shot-id" });
  }
});

export const DraftPackageSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  demandId: NonEmptyIdSchema,
  revisionProposalId: NonEmptyIdSchema.nullable(),
  briefId: NonEmptyIdSchema,
  platformVariant: StudioPlatformVariantSchema.optional(),
  blueprintId: NonEmptyIdSchema,
  blueprintDigest: DigestSchema,
  outputDigest: DigestSchema,
  outputPath: NonEmptyIdSchema,
  durationSeconds: z.number().min(6).max(60),
  sourceMap: z.array(DraftSourceMapEntrySchema).min(1),
  captionCues: z.array(StudioCaptionCueSchema).max(500).optional(),
  captionStyle: StudioCaptionStyleSchema.optional(),
  transitionPlan: StudioTransitionPlanSchema.optional(),
  beatSyncPlan: StudioBeatSyncPlanSchema.optional(),
  musicMixPlan: StudioMusicMixPlanSchema.optional(),
  qualityReport: StudioDraftQualityReportSchema.optional(),
  captionRevision: StudioCaptionRevisionBindingSchema.optional(),
  approvedCaptionImport: StudioApprovedCaptionImportBindingSchema.optional(),
  shareOptimization: StudioShareOptimizationBindingSchema.optional(),
  visualEvidence: StudioDraftVisualEvidenceSchema.optional(),
  sourceReuseMode: z.literal("same-source-segments").optional(),
  status: z.enum(["reviewable", "adopted", "rejected", "changes-requested", "invalidated"]),
  capability: StudioCapabilitySchema,
  externalEdit: ExternalEditReturnedDraftBindingSchema.optional(),
  publishClaimed: z.literal(false),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (!unique(value.sourceMap.map((row) => row.shotId))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceMap"], message: "duplicate-shot-id" });
  }
  if (!unique(value.sourceMap.map((row) => row.sourceAssetId)) && value.sourceReuseMode !== "same-source-segments") {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceMap"], message: "duplicate-source-asset-id" });
  }
  if (value.sourceReuseMode === "same-source-segments" &&
      !unique(value.sourceMap.map((row) => `${row.sourceAssetId}:${row.sourceStartMs}:${row.sourceEndMs}`))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceMap"], message: "duplicate-source-interval" });
  }
  for (const [index, cue] of (value.captionCues ?? []).entries()) {
    const source = value.sourceMap.find((candidate) =>
      candidate.sourceAssetId === cue.sourceAssetId
      && candidate.sourceDigest === cue.sourceDigest
      && candidate.sourceStartMs <= cue.sourceStartMs
      && candidate.sourceEndMs >= cue.sourceEndMs);
    if (!source || cue.timelineEndMs > value.durationSeconds * 1_000 + 1) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["captionCues", index], message: "caption-cue-source-binding-mismatch" });
    }
  }
  for (const [index, evidence] of (value.visualEvidence?.items ?? []).entries()) {
    const source = value.sourceMap.find((candidate) =>
      candidate.shotId === evidence.shotId
      && candidate.sourceAssetId === evidence.sourceAssetId
      && candidate.sourceDigest === evidence.sourceDigest
      && candidate.sourceStartMs <= evidence.sourceStartMs
      && candidate.sourceEndMs >= evidence.sourceEndMs);
    if (!source) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["visualEvidence", "items", index],
        message: "draft-visual-evidence-source-binding-mismatch",
      });
    }
  }
  if (((value.captionCues?.length ?? 0) > 0) !== (value.captionStyle !== undefined)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["captionStyle"], message: "caption-style-and-cues-must-match" });
  }
});

export const FeedbackEventSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  draftId: NonEmptyIdSchema,
  action: z.enum(["adopt", "reject", "request-changes"]),
  reason: NonEmptyIdSchema.nullable(),
  actorId: NonEmptyIdSchema,
  publishClaimed: z.literal(false),
  createdAt: DateTimeSchema,
}).strict();

export const StudioDistributionEventSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  draftId: NonEmptyIdSchema,
  action: z.enum(["download-started", "system-share-invoked", "share-fallback-download"]),
  platformVariant: StudioPlatformVariantSchema.nullable(),
  actorId: NonEmptyIdSchema,
  publishClaimed: z.literal(false),
  externalDeliveryClaimed: z.literal(false),
  createdAt: DateTimeSchema,
}).strict();

export const CustomerDraftFeedbackSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  demandId: NonEmptyIdSchema,
  briefId: NonEmptyIdSchema,
  draftId: NonEmptyIdSchema,
  satisfaction: z.enum(["satisfied", "partially-satisfied", "unsatisfied"]),
  issueCategories: z.array(z.enum([
    "content-direction",
    "shot-selection",
    "pacing",
    "copy",
    "style",
    "fact-error",
    "other",
  ])).refine(unique, "duplicate-issue-category"),
  comment: z.string().trim().min(1).max(STUDIO_INPUT_LIMITS.feedbackComment).nullable(),
  mustKeep: StudioStructuredItemsSchema.refine(unique, "duplicate-must-keep"),
  mustRemove: StudioStructuredItemsSchema.refine(unique, "duplicate-must-remove"),
  needsReshoot: StudioStructuredItemsSchema.refine(unique, "duplicate-needs-reshoot"),
  submittedBy: z.enum(["customer-self-declared", "operator-on-behalf"]),
  actorId: NonEmptyIdSchema,
  publishClaimed: z.literal(false),
  createdAt: DateTimeSchema,
}).strict();

export const RevisionProposalSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  demandId: NonEmptyIdSchema,
  feedbackId: NonEmptyIdSchema,
  parentDraftId: NonEmptyIdSchema,
  parentBriefId: NonEmptyIdSchema,
  understoodFeedback: NonEmptyIdSchema,
  plannedChanges: z.array(NonEmptyIdSchema),
  retainedElements: z.array(NonEmptyIdSchema),
  unsupportedRequests: z.array(NonEmptyIdSchema),
  captureGaps: z.array(NonEmptyIdSchema),
  status: z.enum(["proposed", "confirmed", "rejected", "superseded", "executed"]),
  confirmedByActorId: NonEmptyIdSchema.nullable(),
  confirmedAt: DateTimeSchema.nullable(),
  capability: StudioCapabilitySchema,
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  const confirmed = value.status === "confirmed" || value.status === "executed";
  if (confirmed !== (value.confirmedByActorId !== null && value.confirmedAt !== null)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["confirmedAt"],
      message: "proposal-confirmation-state-mismatch",
    });
  }
});

export const CaptureEnvelopeV1Schema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: z.literal(1),
  requestId: NonEmptyIdSchema,
  mediaKind: z.enum(["video", "image", "audio"]),
  fileName: NonEmptyIdSchema,
  bytes: z.instanceof(Uint8Array).refine((bytes) => bytes.byteLength > 0, "capture-bytes-required"),
  binding: z.object({
    customerId: NonEmptyIdSchema,
    sessionId: NonEmptyIdSchema,
    verified: z.literal(true),
  }).strict(),
  adapter: z.object({
    name: NonEmptyIdSchema,
    version: NonEmptyIdSchema,
  }).strict(),
  capturedAt: DateTimeSchema.nullable(),
  viewPermission: StudioPermissionStateSchema.default("review-required"),
  usePermission: StudioPermissionStateSchema.default("review-required"),
}).strict().superRefine((value, ctx) => {
  if (value.binding.customerId !== value.customerId) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["binding", "customerId"], message: "capture-customer-binding-mismatch" });
  }
  if (value.binding.sessionId !== value.sessionId) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["binding", "sessionId"], message: "capture-session-binding-mismatch" });
  }
});

export type StudioContentType = z.infer<typeof StudioContentTypeSchema>;
export type StudioPermissionState = z.infer<typeof StudioPermissionStateSchema>;
export type StudioEvidenceKind = z.infer<typeof StudioEvidenceKindSchema>;
export type StudioEvidenceRecord = z.infer<typeof StudioEvidenceRecordSchema>;
export type StudioEvidenceDegradedReason = z.infer<typeof StudioEvidenceDegradedReasonSchema>;
export type MatchEvidenceSource = z.infer<typeof MatchEvidenceSourceSchema>;
export type StudioCapability = z.infer<typeof StudioCapabilitySchema>;
export type StudioMediaAsset = z.infer<typeof StudioMediaAssetSchema>;
export type MaterialCard = z.infer<typeof MaterialCardSchema>;
export type MaterialInventory = z.infer<typeof MaterialInventorySchema>;
export type ContentDemand = z.infer<typeof ContentDemandSchema>;
export type MatchAssessment = z.infer<typeof MatchAssessmentSchema>;
export type ContentOpportunity = z.infer<typeof ContentOpportunitySchema>;
export type StudioCreativeBrief = z.infer<typeof StudioCreativeBriefSchema>;
export type GapList = z.infer<typeof GapListSchema>;
export type StudioEditorialDirection = z.infer<typeof StudioEditorialDirectionSchema>;
export type StudioBlueprintShotRole = z.infer<typeof StudioBlueprintShotRoleSchema>;
export type StudioCaptionCue = z.infer<typeof StudioCaptionCueSchema>;
export type StudioCaptionStyle = z.infer<typeof StudioCaptionStyleSchema>;
export type StudioTransitionPlan = z.infer<typeof StudioTransitionPlanSchema>;
export type StudioBeatSyncPlan = z.infer<typeof StudioBeatSyncPlanSchema>;
export type StudioMusicRights = z.infer<typeof StudioMusicRightsSchema>;
export type StudioMusicMixPlan = z.infer<typeof StudioMusicMixPlanSchema>;
export type StudioDraftQualityCheck = z.infer<typeof StudioDraftQualityCheckSchema>;
export type StudioDraftQualityReport = z.infer<typeof StudioDraftQualityReportSchema>;
export type StudioCaptionRevisionBinding = z.infer<typeof StudioCaptionRevisionBindingSchema>;
export type StudioDraftVisualEvidence = z.infer<typeof StudioDraftVisualEvidenceSchema>;
export type EditBlueprint = z.infer<typeof EditBlueprintSchema>;
export type DraftPackage = z.infer<typeof DraftPackageSchema>;
export type ExternalEditHandoffStatus = z.infer<typeof ExternalEditHandoffStatusSchema>;
export type ExternalEditHandoffMode = z.infer<typeof ExternalEditHandoffModeSchema>;
export type StudioPlatformVariant = z.infer<typeof StudioPlatformVariantSchema>;
export type ExternalEditSourceAssetBinding = z.infer<typeof ExternalEditSourceAssetBindingSchema>;
export type ExternalEditHandoff = z.infer<typeof ExternalEditHandoffSchema>;
export type ExternalEditPackage = z.infer<typeof ExternalEditPackageSchema>;
export type ExternalEditReturnCandidate = z.infer<typeof ExternalEditReturnCandidateSchema>;
export type ExternalEditReturnedDraftBinding = z.infer<typeof ExternalEditReturnedDraftBindingSchema>;
export type FeedbackEvent = z.infer<typeof FeedbackEventSchema>;
export type StudioDistributionEvent = z.infer<typeof StudioDistributionEventSchema>;
export type CustomerDraftFeedback = z.infer<typeof CustomerDraftFeedbackSchema>;
export type RevisionProposal = z.infer<typeof RevisionProposalSchema>;
export type CaptureEnvelopeV1 = z.infer<typeof CaptureEnvelopeV1Schema>;
export type CaptureEnvelopeV1Input = z.input<typeof CaptureEnvelopeV1Schema>;
