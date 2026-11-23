import { z } from "zod";

import { StoryTypeSchema, VideoVariantSchema } from "./content-editor";

const NonEmptyIdSchema = z.string().trim().min(1);
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const DateTimeSchema = z.string().datetime();
const AbsolutePathSchema = z.string().trim().min(1).refine(
  (value) => value.startsWith("/"),
  "absolute-source-path-required",
);

const allUnique = (values: readonly string[]): boolean => new Set(values).size === values.length;

export const RealMaterialRoleSchema = z.enum(["current", "historical", "reference"]);
export const RealMaterialMediaKindSchema = z.enum(["video", "image", "audio"]);
export const RealMaterialAllowedUseSchema = z.enum([
  "story-analysis",
  "render",
  "reference-analysis",
  "still-motion",
]);
export const RealMaterialOwnershipSchema = z.enum([
  "customer-owned",
  "licensed",
  "external-style-only",
]);
export const RealMaterialReferencePolicySchema = z.enum(["customer-owned", "external-style-only"]);
export const RealMaterialFactKindSchema = z.enum([
  "product-id",
  "process-stage",
  "first-person-choice",
  "emotion",
  "opinion",
  "supporting-evidence",
]);
export const RealMaterialStoryStatusSchema = z.enum([
  "ready",
  "insufficient-evidence",
  "blocked-by-capability",
]);
export const RealMaterialRunStatusSchema = z.enum([
  "awaiting-decision",
  "rendered",
  "failed",
]);
export const RealMaterialEvidenceScopeSchema = z.enum([
  "local-real-media",
  "local-synthetic-test",
]);
export const RealMaterialCapabilityLayerSchema = z.enum([
  "media",
  "scene",
  "transcript",
  "vision",
  "embedding",
]);

export const RealMaterialStoryErrorCodeSchema = z.enum([
  "insufficient-process-evidence",
  "insufficient-inner-journey-evidence",
  "insufficient-persona-evidence",
  "media-capability-unavailable",
  "scene-capability-unavailable",
  "transcript-capability-unavailable",
  "vision-capability-unavailable",
  "embedding-capability-unavailable",
]);

export const RealMaterialConfirmedFactSchema = z.object({
  kind: RealMaterialFactKindSchema,
  value: z.string().trim().min(1),
  sourceStartMs: z.number().int().nonnegative().nullable(),
  sourceEndMs: z.number().int().positive().nullable(),
  confirmedBy: NonEmptyIdSchema,
  confirmedAt: DateTimeSchema,
}).strict();

export const RealMaterialManifestInputSchema = z.object({
  inputId: NonEmptyIdSchema,
  role: RealMaterialRoleSchema,
  mediaKind: RealMaterialMediaKindSchema,
  sourcePath: AbsolutePathSchema,
  sourceDigest: DigestSchema,
  permissionState: z.literal("allowed"),
  allowedUses: z.array(RealMaterialAllowedUseSchema).min(1),
  declaredOwnership: RealMaterialOwnershipSchema,
  confirmedFacts: z.array(RealMaterialConfirmedFactSchema),
}).strict().superRefine((value, ctx) => {
  if (!allUnique(value.allowedUses)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["allowedUses"],
      message: "duplicate-real-material-allowed-use",
    });
  }

  for (const [index, fact] of value.confirmedFacts.entries()) {
    if (value.mediaKind === "image") {
      if (fact.sourceStartMs !== null || fact.sourceEndMs !== null) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["confirmedFacts", index],
          message: "image-confirmed-fact-bounds-must-be-null",
        });
      }
      if (["first-person-choice", "emotion", "opinion"].includes(fact.kind)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["confirmedFacts", index, "kind"],
          message: "image-cannot-confirm-spoken-or-emotional-fact",
        });
      }
      continue;
    }

    if (fact.sourceStartMs === null || fact.sourceEndMs === null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["confirmedFacts", index],
        message: "timed-media-confirmed-fact-bounds-required",
      });
      continue;
    }
    if (fact.sourceEndMs <= fact.sourceStartMs) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["confirmedFacts", index, "sourceEndMs"],
        message: "confirmed-fact-end-must-follow-start",
      });
    }
  }
});

const REQUIRED_VARIANTS = new Set(["editorial-master", "xiaohongshu", "douyin", "wechat-channels"]);

export const RealMaterialRunManifestSchema = z.object({
  schemaVersion: z.literal(1),
  runId: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  actorId: NonEmptyIdSchema,
  createdAt: DateTimeSchema,
  purpose: z.literal("real-material-acceptance"),
  inputs: z.array(RealMaterialManifestInputSchema).min(1),
  referencePolicy: RealMaterialReferencePolicySchema,
  requestedStoryTypes: z.array(StoryTypeSchema).min(1),
  requestedVariants: z.array(VideoVariantSchema),
}).strict().superRefine((value, ctx) => {
  const inputIds = value.inputs.map(({ inputId }) => inputId);
  if (!allUnique(inputIds)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["inputs"],
      message: "duplicate-real-material-input-id",
    });
  }

  if (!allUnique(value.requestedStoryTypes)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["requestedStoryTypes"],
      message: "duplicate-requested-story-type",
    });
  }

  const variants = new Set(value.requestedVariants);
  if (
    value.requestedVariants.length !== REQUIRED_VARIANTS.size
    || variants.size !== REQUIRED_VARIANTS.size
    || [...REQUIRED_VARIANTS].some((variant) => !variants.has(variant as z.infer<typeof VideoVariantSchema>))
  ) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["requestedVariants"],
      message: "four-real-material-variants-required",
    });
  }

  const referenceInputs = value.inputs.filter(({ role }) => role === "reference");
  if (referenceInputs.length > 1) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["inputs"],
      message: "single-reference-input-required",
    });
  }
  for (const [index, input] of value.inputs.entries()) {
    if (input.role === "reference") {
      if (!input.allowedUses.includes("reference-analysis")) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["inputs", index, "allowedUses"],
          message: "reference-analysis-use-required",
        });
      }
      if (input.declaredOwnership !== value.referencePolicy) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["inputs", index, "declaredOwnership"],
          message: "reference-policy-ownership-mismatch",
        });
      }
      if (input.confirmedFacts.length > 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["inputs", index, "confirmedFacts"],
          message: "reference-input-cannot-carry-story-facts",
        });
      }
      continue;
    }

    if (input.allowedUses.includes("reference-analysis")) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["inputs", index, "allowedUses"],
        message: "non-reference-input-cannot-use-reference-analysis",
      });
    }
    if (input.declaredOwnership === "external-style-only") {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["inputs", index, "declaredOwnership"],
        message: "story-input-cannot-be-external-style-only",
      });
    }
  }
});

export const RealMaterialRunDecisionSchema = z.object({
  schemaVersion: z.literal(1),
  decisionId: NonEmptyIdSchema,
  runId: NonEmptyIdSchema,
  reviewPacketDigest: DigestSchema,
  selectedStoryTypes: z.array(StoryTypeSchema).min(1),
  selectedReferenceRuleIds: z.array(NonEmptyIdSchema).min(1),
  mappingPlan: z.object({
    id: NonEmptyIdSchema,
    version: z.number().int().positive(),
    digest: DigestSchema,
  }).strict(),
  confirmedBy: NonEmptyIdSchema,
  confirmedAt: DateTimeSchema,
  operationKey: NonEmptyIdSchema,
}).strict().superRefine((value, ctx) => {
  if (!allUnique(value.selectedStoryTypes)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["selectedStoryTypes"],
      message: "duplicate-selected-story-type",
    });
  }
  if (!allUnique(value.selectedReferenceRuleIds)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["selectedReferenceRuleIds"],
      message: "duplicate-selected-reference-rule-id",
    });
  }
});

export const RealMaterialCapabilityLayerResultSchema = z.object({
  layer: RealMaterialCapabilityLayerSchema,
  status: z.enum(["ready", "degraded", "blocked"]),
  evidenceCodes: z.array(NonEmptyIdSchema).refine(allUnique, "duplicate-capability-evidence-code"),
  message: z.string().trim().min(1),
}).strict();

const REQUIRED_CAPABILITY_LAYERS = new Set(["media", "scene", "transcript", "vision", "embedding"]);

export const RealMaterialCapabilityReportSchema = z.object({
  schemaVersion: z.literal(1),
  runId: NonEmptyIdSchema,
  generatedAt: DateTimeSchema,
  layers: z.array(RealMaterialCapabilityLayerResultSchema),
}).strict().superRefine((value, ctx) => {
  const layers = value.layers.map(({ layer }) => layer);
  if (
    layers.length !== REQUIRED_CAPABILITY_LAYERS.size
    || !allUnique(layers)
    || [...REQUIRED_CAPABILITY_LAYERS].some((layer) => !layers.includes(
      layer as z.infer<typeof RealMaterialCapabilityLayerSchema>,
    ))
  ) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["layers"],
      message: "five-real-material-capability-layers-required",
    });
  }
});

export const RealMaterialStoryReadinessSchema = z.object({
  storyType: StoryTypeSchema,
  status: RealMaterialStoryStatusSchema,
  evidence: z.array(z.string().trim().min(1)),
  missingEvidence: z.array(z.string().trim().min(1)),
  sourceIds: z.array(NonEmptyIdSchema).refine(allUnique, "duplicate-story-source-id"),
  errorCode: RealMaterialStoryErrorCodeSchema.nullable(),
  message: z.string().trim().min(1),
}).strict().superRefine((value, ctx) => {
  if (value.status === "ready" && value.errorCode !== null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["errorCode"],
      message: "ready-story-must-not-have-error-code",
    });
  }
  if (value.status !== "ready" && value.errorCode === null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["errorCode"],
      message: "unready-story-requires-error-code",
    });
  }
});

export const RealMaterialInputReceiptSchema = z.object({
  inputId: NonEmptyIdSchema,
  role: RealMaterialRoleSchema,
  mediaKind: RealMaterialMediaKindSchema,
  digest: DigestSchema,
}).strict();

export const RealMaterialReferenceRuleResultSchema = z.object({
  ruleId: NonEmptyIdSchema,
  status: z.enum(["matched", "substituted", "unachievable"]),
  message: z.string().trim().min(1),
}).strict();

export const RealMaterialOutputSchema = z.object({
  storyType: StoryTypeSchema,
  variant: VideoVariantSchema,
  artifactId: NonEmptyIdSchema,
  artifactDigest: DigestSchema,
  timelineDigest: DigestSchema,
  durationMs: z.number().int().positive(),
  width: z.number().int().positive(),
  height: z.number().int().positive(),
  sourceInputIds: z.array(NonEmptyIdSchema).min(1).refine(allUnique, "duplicate-output-source-input-id"),
  referenceBytesUsed: z.literal(false),
}).strict();

export const RealMaterialRunReportSchema = z.object({
  schemaVersion: z.literal(1),
  runId: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  status: RealMaterialRunStatusSchema,
  evidenceScope: RealMaterialEvidenceScopeSchema,
  generatedAt: DateTimeSchema,
  reviewPacketDigest: DigestSchema.nullable(),
  decisionId: NonEmptyIdSchema.nullable(),
  inputReceipts: z.array(RealMaterialInputReceiptSchema).min(1),
  capability: RealMaterialCapabilityReportSchema,
  storyResults: z.array(RealMaterialStoryReadinessSchema),
  referenceRuleResults: z.array(RealMaterialReferenceRuleResultSchema),
  outputs: z.array(RealMaterialOutputSchema),
  publishedClaimed: z.literal(false),
}).strict().superRefine((value, ctx) => {
  if (value.capability.runId !== value.runId) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["capability", "runId"],
      message: "capability-run-id-mismatch",
    });
  }
  if (!allUnique(value.inputReceipts.map(({ inputId }) => inputId))) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["inputReceipts"],
      message: "duplicate-report-input-id",
    });
  }
  if (!allUnique(value.storyResults.map(({ storyType }) => storyType))) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["storyResults"],
      message: "duplicate-report-story-type",
    });
  }
  if (!allUnique(value.referenceRuleResults.map(({ ruleId }) => ruleId))) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["referenceRuleResults"],
      message: "duplicate-reference-rule-result",
    });
  }
  if (value.status === "awaiting-decision") {
    if (value.outputs.length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["outputs"],
        message: "awaiting-decision-report-cannot-have-outputs",
      });
    }
    if (value.decisionId !== null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["decisionId"],
        message: "awaiting-decision-report-cannot-have-decision-id",
      });
    }
  }

  const outputKeys = value.outputs.map(({ storyType, variant }) => `${storyType}:${variant}`);
  if (!allUnique(outputKeys)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["outputs"],
      message: "duplicate-real-material-output",
    });
  }

  if (value.status === "rendered") {
    if (value.decisionId === null || value.outputs.length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["outputs"],
        message: "rendered-report-requires-decision-and-outputs",
      });
    }
    const outputsByStory = new Map<string, Set<string>>();
    for (const output of value.outputs) {
      const variants = outputsByStory.get(output.storyType) ?? new Set<string>();
      variants.add(output.variant);
      outputsByStory.set(output.storyType, variants);
    }
    for (const variants of outputsByStory.values()) {
      if (
        variants.size !== REQUIRED_VARIANTS.size
        || [...REQUIRED_VARIANTS].some((variant) => !variants.has(variant))
      ) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["outputs"],
          message: "rendered-story-requires-four-variants",
        });
      }
    }
  }
});

export type RealMaterialRole = z.infer<typeof RealMaterialRoleSchema>;
export type RealMaterialMediaKind = z.infer<typeof RealMaterialMediaKindSchema>;
export type RealMaterialAllowedUse = z.infer<typeof RealMaterialAllowedUseSchema>;
export type RealMaterialOwnership = z.infer<typeof RealMaterialOwnershipSchema>;
export type RealMaterialReferencePolicy = z.infer<typeof RealMaterialReferencePolicySchema>;
export type RealMaterialFactKind = z.infer<typeof RealMaterialFactKindSchema>;
export type RealMaterialStoryStatus = z.infer<typeof RealMaterialStoryStatusSchema>;
export type RealMaterialRunStatus = z.infer<typeof RealMaterialRunStatusSchema>;
export type RealMaterialEvidenceScope = z.infer<typeof RealMaterialEvidenceScopeSchema>;
export type RealMaterialCapabilityLayer = z.infer<typeof RealMaterialCapabilityLayerSchema>;
export type RealMaterialStoryErrorCode = z.infer<typeof RealMaterialStoryErrorCodeSchema>;
export type RealMaterialConfirmedFact = z.infer<typeof RealMaterialConfirmedFactSchema>;
export type RealMaterialManifestInput = z.infer<typeof RealMaterialManifestInputSchema>;
export type RealMaterialRunManifest = z.infer<typeof RealMaterialRunManifestSchema>;
export type RealMaterialRunDecision = z.infer<typeof RealMaterialRunDecisionSchema>;
export type RealMaterialCapabilityLayerResult = z.infer<typeof RealMaterialCapabilityLayerResultSchema>;
export type RealMaterialCapabilityReport = z.infer<typeof RealMaterialCapabilityReportSchema>;
export type RealMaterialStoryReadiness = z.infer<typeof RealMaterialStoryReadinessSchema>;
export type RealMaterialInputReceipt = z.infer<typeof RealMaterialInputReceiptSchema>;
export type RealMaterialReferenceRuleResult = z.infer<typeof RealMaterialReferenceRuleResultSchema>;
export type RealMaterialOutput = z.infer<typeof RealMaterialOutputSchema>;
export type RealMaterialRunReport = z.infer<typeof RealMaterialRunReportSchema>;
