import { z } from "zod";

import {
  StudioCapabilitySchema,
  StudioPermissionStateSchema,
} from "./private-content-studio";

const NonEmptyIdSchema = z.string().trim().min(1);
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const PositiveVersionSchema = z.number().int().positive();
const DateTimeSchema = z.string().datetime();
const NonNegativeMillisecondsSchema = z.number().int().nonnegative();
const PositiveMillisecondsSchema = z.number().int().positive();
const SafeCodeSchema = z.string().regex(/^[a-z0-9][a-z0-9._:-]{0,127}$/);
const unique = (values: readonly string[]) => new Set(values).size === values.length;
const sortedUniqueNumbers = (values: readonly number[]) =>
  values.every((value, index) => index === 0 || value > values[index - 1]);

export const REFERENCE_TRANSITION_CONFIDENCE_THRESHOLD = 0.6 as const;

export const ReferenceSourceKindSchema = z.enum(["self-owned", "external-style-only"]);
export const ReferenceCollectionModeSchema = z.enum(["dedicated-upload", "historical-pointer"]);
export const ReferenceReusableElementSchema = z.enum(["video-clips", "audio-track", "spoken-content"]);
export const ReferenceTransitionKindSchema = z.enum(["cut", "fade", "crossfade", "unsupported"]);
export const ReferenceMappingStatusSchema = z.enum(["matched", "substituted", "unachievable"]);
export const ReferenceVariantKindSchema = z.enum(["editorial-master", "xiaohongshu", "douyin", "wechat-channels"]);
export const ReferenceAnalysisStatusSchema = z.enum(["complete", "degraded"]);
export const ReferenceRuleKindSchema = z.enum(["rhythm", "transition", "narrative", "audio"]);
export const ReferenceSourceProvenanceSchema = z.enum([
  "customer-current",
  "customer-authorized-history",
  "self-owned-reference",
]);

const ReferenceSampleBindingSchema = z.object({
  id: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceKind: ReferenceSourceKindSchema,
  collectionMode: ReferenceCollectionModeSchema,
  candidateScope: z.literal("reference-only"),
}).strict();

const ReferenceRightsBindingSchema = z.object({
  id: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  referenceSampleId: NonEmptyIdSchema,
  referenceSampleVersion: PositiveVersionSchema,
  referenceSampleDigest: DigestSchema,
  sourceKind: ReferenceSourceKindSchema,
  reusableElements: z.array(ReferenceReusableElementSchema).refine(
    unique,
    "duplicate-reference-reusable-element",
  ),
  contentDigest: DigestSchema,
}).strict();

const ScopedContentBindingSchema = z.object({
  id: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  contentDigest: DigestSchema,
}).strict();

const ConfirmationBindingSchema = z.object({
  receiptId: NonEmptyIdSchema,
  actorId: NonEmptyIdSchema,
  confirmedAt: DateTimeSchema,
}).strict();

const ReferencePermissionIdentitySchema = z.object({
  id: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  epoch: PositiveVersionSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
}).strict();

const ConfirmedStyleSelectionBindingSchema = ScopedContentBindingSchema.extend({
  status: z.enum(["confirmed", "invalidated"]),
  confirmation: ConfirmationBindingSchema,
}).strict().superRefine((value, ctx) => {
  if (value.status !== "confirmed") {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["status"],
      message: "confirmed-reference-style-selection-required",
    });
  }
});

const ConfirmedMappingPlanBindingSchema = ScopedContentBindingSchema.extend({
  permissionSetDigest: DigestSchema,
  permissions: z.array(ReferencePermissionIdentitySchema).min(1),
  status: z.enum(["draft", "confirmed", "invalidated"]),
  confirmation: ConfirmationBindingSchema,
}).strict().superRefine((value, ctx) => {
  if (value.status !== "confirmed") {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["status"],
      message: "confirmed-reference-mapping-plan-required",
    });
  }
});

const ExecutionReportBindingSchema = z.object({
  id: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
}).strict();

function validateSampleRightsBinding(
  value: {
    sample: z.infer<typeof ReferenceSampleBindingSchema>;
    rights: z.infer<typeof ReferenceRightsBindingSchema>;
  },
  ctx: z.RefinementCtx,
) {
  if (value.sample.customerId !== value.rights.customerId ||
      value.sample.sessionId !== value.rights.sessionId) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["rights"],
      message: "reference-workflow-tenant-binding-mismatch",
    });
  }
  if (value.sample.id !== value.rights.referenceSampleId ||
      value.sample.version !== value.rights.referenceSampleVersion ||
      value.sample.sourceDigest !== value.rights.referenceSampleDigest) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["rights"],
      message: "reference-rights-sample-binding-mismatch",
    });
  }
  if (value.sample.sourceKind !== value.rights.sourceKind) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["rights", "sourceKind"],
      message: "reference-rights-source-kind-mismatch",
    });
  }
  if (value.rights.sourceKind === "external-style-only" && value.rights.reusableElements.length > 0) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["rights", "reusableElements"],
      message: "external-reference-reusable-elements-forbidden",
    });
  }
}

function isBoundReferenceSource(
  sourceAssetId: string,
  sourceDigest: string,
  sample: z.infer<typeof ReferenceSampleBindingSchema>,
) {
  return sourceAssetId === sample.sourceAssetId && sourceDigest === sample.sourceDigest;
}

function validateScopedBinding(
  customerId: string,
  sessionId: string,
  bindings: Record<string, { customerId: string; sessionId: string }>,
  ctx: z.RefinementCtx,
) {
  for (const [name, binding] of Object.entries(bindings)) {
    if (binding.customerId !== customerId || binding.sessionId !== sessionId) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["bindings", name],
        message: "reference-workflow-tenant-binding-mismatch",
      });
    }
  }
}

const AnalysisBindingsSchema = z.object({
  sample: ReferenceSampleBindingSchema,
  rights: ReferenceRightsBindingSchema,
}).strict().superRefine(validateSampleRightsBinding);

const SelectionBindingsSchema = z.object({
  sample: ReferenceSampleBindingSchema,
  rights: ReferenceRightsBindingSchema,
  analysis: ScopedContentBindingSchema,
}).strict().superRefine((value, ctx) => {
  validateSampleRightsBinding(value, ctx);
  validateScopedBinding(value.sample.customerId, value.sample.sessionId, {
    analysis: value.analysis,
  }, ctx);
});

const MappingBindingsSchema = z.object({
  sample: ReferenceSampleBindingSchema,
  rights: ReferenceRightsBindingSchema,
  analysis: ScopedContentBindingSchema,
  styleSelection: ConfirmedStyleSelectionBindingSchema,
}).strict().superRefine((value, ctx) => {
  validateSampleRightsBinding(value, ctx);
  validateScopedBinding(value.sample.customerId, value.sample.sessionId, {
    analysis: value.analysis,
    styleSelection: value.styleSelection,
  }, ctx);
});

const RenderBindingsSchema = z.object({
  sample: ReferenceSampleBindingSchema,
  rights: ReferenceRightsBindingSchema,
  analysis: ScopedContentBindingSchema,
  styleSelection: ConfirmedStyleSelectionBindingSchema,
  mappingPlan: ConfirmedMappingPlanBindingSchema,
}).strict().superRefine((value, ctx) => {
  validateSampleRightsBinding(value, ctx);
  validateScopedBinding(value.sample.customerId, value.sample.sessionId, {
    analysis: value.analysis,
    styleSelection: value.styleSelection,
    mappingPlan: value.mappingPlan,
  }, ctx);
});

const MobileBindingsSchema = z.object({
  sample: ReferenceSampleBindingSchema,
  rights: ReferenceRightsBindingSchema,
  analysis: ScopedContentBindingSchema,
  styleSelection: ConfirmedStyleSelectionBindingSchema,
  mappingPlan: ConfirmedMappingPlanBindingSchema,
  executionReport: ExecutionReportBindingSchema,
}).strict().superRefine((value, ctx) => {
  validateSampleRightsBinding(value, ctx);
  validateScopedBinding(value.sample.customerId, value.sample.sessionId, {
    analysis: value.analysis,
    styleSelection: value.styleSelection,
    mappingPlan: value.mappingPlan,
    executionReport: value.executionReport,
  }, ctx);
});

const addIntervalIssue = (
  value: { startMs: number; endMs: number },
  ctx: z.RefinementCtx,
) => {
  if (value.endMs <= value.startMs) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["endMs"],
      message: "reference-end-must-follow-start",
    });
  }
};

export const ReferenceSampleSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceKind: ReferenceSourceKindSchema,
  collectionMode: ReferenceCollectionModeSchema,
  candidateScope: z.literal("reference-only"),
  status: z.enum(["pending-rights", "ready", "invalidated"]),
  viewPermission: StudioPermissionStateSchema,
  usePermission: StudioPermissionStateSchema,
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (value.collectionMode === "dedicated-upload" && value.usePermission !== "review-required") {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["usePermission"],
      message: "dedicated-reference-use-permission-must-require-review",
    });
  }
});

export const ReferenceRightsDeclarationSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  contentDigest: DigestSchema,
  sample: ReferenceSampleBindingSchema,
  sourceKind: ReferenceSourceKindSchema,
  reusableElements: z.array(ReferenceReusableElementSchema).refine(unique, "duplicate-reference-reusable-element"),
  actorId: NonEmptyIdSchema,
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (value.sample.customerId !== value.customerId) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["sample", "customerId"],
      message: "reference-rights-customer-binding-mismatch",
    });
  }
  if (value.sample.sessionId !== value.sessionId) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["sample", "sessionId"],
      message: "reference-rights-session-binding-mismatch",
    });
  }
  if (value.sample.sourceKind !== value.sourceKind) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["sourceKind"],
      message: "reference-rights-source-kind-mismatch",
    });
  }
  if (value.sourceKind === "external-style-only" && value.reusableElements.length > 0) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["reusableElements"],
      message: "external-reference-reusable-elements-forbidden",
    });
  }
});

const ReferenceSectionSchema = z.object({
  id: NonEmptyIdSchema,
  startMs: NonNegativeMillisecondsSchema,
  endMs: PositiveMillisecondsSchema,
  summary: z.string().trim().min(1).max(500),
}).strict().superRefine(addIntervalIssue);

const ReferenceTranscriptSpanSchema = z.object({
  id: NonEmptyIdSchema,
  startMs: NonNegativeMillisecondsSchema,
  endMs: PositiveMillisecondsSchema,
  text: z.string().trim().min(1).max(2_000),
}).strict().superRefine(addIntervalIssue);

const ReferenceTransitionEvidenceSchema = z.object({
  id: NonEmptyIdSchema,
  atMs: NonNegativeMillisecondsSchema,
  kind: ReferenceTransitionKindSchema,
  confidence: z.number().min(0).max(1),
}).strict().superRefine((value, ctx) => {
  if (value.kind !== "unsupported" && value.confidence < REFERENCE_TRANSITION_CONFIDENCE_THRESHOLD) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["kind"],
      message: "low-confidence-reference-transition-must-be-unsupported",
    });
  }
});

const ReferenceStyleRuleSchema = z.object({
  id: NonEmptyIdSchema,
  kind: ReferenceRuleKindSchema,
  description: z.string().trim().min(1).max(500),
  referenceStartMs: NonNegativeMillisecondsSchema,
  referenceEndMs: PositiveMillisecondsSchema,
  evidenceIds: z.array(NonEmptyIdSchema).min(1).refine(unique, "duplicate-reference-evidence-id"),
}).strict().superRefine((value, ctx) => {
  if (value.referenceEndMs <= value.referenceStartMs) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["referenceEndMs"],
      message: "reference-end-must-follow-start",
    });
  }
});

export const ReferenceStyleAnalysisSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  contentDigest: DigestSchema,
  bindings: AnalysisBindingsSchema,
  cuts: z.array(NonNegativeMillisecondsSchema).refine(sortedUniqueNumbers, "reference-cuts-must-be-sorted-and-unique"),
  sections: z.array(ReferenceSectionSchema),
  transcriptSpans: z.array(ReferenceTranscriptSpanSchema),
  beatMarkers: z.array(NonNegativeMillisecondsSchema).refine(
    sortedUniqueNumbers,
    "reference-beat-markers-must-be-sorted-and-unique",
  ),
  transitions: z.array(ReferenceTransitionEvidenceSchema),
  rules: z.array(ReferenceStyleRuleSchema).min(1),
  warnings: z.array(SafeCodeSchema).refine(unique, "duplicate-reference-analysis-warning"),
  confidence: z.number().min(0).max(1),
  status: ReferenceAnalysisStatusSchema,
  capability: StudioCapabilitySchema,
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  validateScopedBinding(value.customerId, value.sessionId, {
    sample: value.bindings.sample,
    rights: value.bindings.rights,
  }, ctx);
  if (value.status === "degraded" && value.warnings.length === 0) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["warnings"],
      message: "degraded-reference-analysis-warning-required",
    });
  }
  if (!unique(value.rules.map((rule) => rule.id))) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["rules"],
      message: "duplicate-reference-rule-id",
    });
  }
});

export const ReferenceStyleSelectionSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  contentDigest: DigestSchema,
  bindings: SelectionBindingsSchema,
  selectedRuleIds: z.array(NonEmptyIdSchema).min(1).refine(unique, "duplicate-selected-reference-rule-id"),
  actorId: NonEmptyIdSchema,
  confirmationReceiptId: NonEmptyIdSchema,
  confirmedAt: DateTimeSchema,
  status: z.enum(["confirmed", "invalidated"]),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  validateScopedBinding(value.customerId, value.sessionId, {
    sample: value.bindings.sample,
    rights: value.bindings.rights,
    analysis: value.bindings.analysis,
  }, ctx);
});

const ReferencePermissionBindingSchema = ReferencePermissionIdentitySchema.extend({
  view: StudioPermissionStateSchema,
  use: StudioPermissionStateSchema,
}).strict();

const ReferenceReuseRightsBindingSchema = z.object({
  id: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  element: z.enum(["video-clips", "audio-track"]),
}).strict();

function validatePermissionBinding(
  value: {
    sourceAssetId: string;
    sourceDigest: string;
    permission: z.infer<typeof ReferencePermissionBindingSchema>;
  },
  ctx: z.RefinementCtx,
) {
  if (value.permission.sourceAssetId !== value.sourceAssetId ||
      value.permission.sourceDigest !== value.sourceDigest) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["permission"],
      message: "reference-source-permission-binding-mismatch",
    });
  }
  if (value.permission.view !== "allowed" || value.permission.use !== "allowed") {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["permission"],
      message: "reference-mapping-use-permission-required",
    });
  }
}

const ReferenceMaterialMappingSchema = z.object({
  id: NonEmptyIdSchema,
  ruleId: NonEmptyIdSchema,
  referenceStartMs: NonNegativeMillisecondsSchema,
  referenceEndMs: PositiveMillisecondsSchema,
  targetRule: z.string().trim().min(1).max(500),
  sourceBindingId: NonEmptyIdSchema.nullable(),
  sourceAssetId: NonEmptyIdSchema.nullable(),
  sourceDigest: DigestSchema.nullable(),
  sourceStartMs: NonNegativeMillisecondsSchema.nullable(),
  sourceEndMs: PositiveMillisecondsSchema.nullable(),
  sourceProvenance: ReferenceSourceProvenanceSchema.nullable(),
  permission: ReferencePermissionBindingSchema.nullable(),
  reuseRights: ReferenceReuseRightsBindingSchema.nullable(),
  status: ReferenceMappingStatusSchema,
  differenceReason: z.string().trim().min(1).max(500).nullable(),
}).strict().superRefine((value, ctx) => {
  if (value.referenceEndMs <= value.referenceStartMs) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["referenceEndMs"],
      message: "reference-end-must-follow-start",
    });
  }

  const coreSourceFields = [
    value.sourceBindingId,
    value.sourceAssetId,
    value.sourceDigest,
    value.sourceStartMs,
    value.sourceEndMs,
    value.sourceProvenance,
    value.permission,
  ];
  const hasCompleteSource = coreSourceFields.every((field) => field !== null);
  const hasAnySource = coreSourceFields.some((field) => field !== null) || value.reuseRights !== null;

  if (value.status === "matched" && !hasCompleteSource) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["sourceBindingId"],
      message: "matched-reference-mapping-source-required",
    });
  }
  if (value.status === "substituted" && !hasCompleteSource) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["sourceBindingId"],
      message: "substituted-reference-mapping-source-required",
    });
  }
  if (value.status === "substituted" && value.differenceReason === null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["differenceReason"],
      message: "substituted-reference-mapping-difference-required",
    });
  }
  if (value.status === "unachievable" && hasAnySource) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["sourceBindingId"],
      message: "unachievable-reference-mapping-source-forbidden",
    });
  }
  if (value.status === "unachievable" && value.differenceReason === null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["differenceReason"],
      message: "unachievable-reference-mapping-difference-required",
    });
  }
  if (hasCompleteSource) {
    if (value.sourceEndMs! <= value.sourceStartMs!) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["sourceEndMs"],
        message: "source-end-must-follow-start",
      });
    }
    validatePermissionBinding({
      sourceAssetId: value.sourceAssetId!,
      sourceDigest: value.sourceDigest!,
      permission: value.permission!,
    }, ctx);
    if (value.sourceProvenance === "self-owned-reference" && value.reuseRights === null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["reuseRights"],
        message: "reference-reuse-rights-required",
      });
    }
    if (value.sourceProvenance !== "self-owned-reference" && value.reuseRights !== null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["reuseRights"],
        message: "customer-source-reference-rights-forbidden",
      });
    }
  }
});

export const ReferenceMaterialMappingPlanSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  contentDigest: DigestSchema,
  permissionSetDigest: DigestSchema,
  bindings: MappingBindingsSchema,
  mappings: z.array(ReferenceMaterialMappingSchema).min(1),
  unresolvedCount: z.number().int().nonnegative(),
  status: z.enum(["draft", "confirmed", "invalidated"]),
  confirmedByActorId: NonEmptyIdSchema.nullable(),
  confirmationReceiptId: NonEmptyIdSchema.nullable(),
  confirmedAt: DateTimeSchema.nullable(),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  validateScopedBinding(value.customerId, value.sessionId, {
    sample: value.bindings.sample,
    rights: value.bindings.rights,
    analysis: value.bindings.analysis,
    styleSelection: value.bindings.styleSelection,
  }, ctx);
  if (!unique(value.mappings.map((mapping) => mapping.id)) ||
      !unique(value.mappings.map((mapping) => mapping.sourceBindingId).filter((id): id is string => id !== null))) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["mappings"],
      message: "duplicate-reference-mapping-or-source-binding-id",
    });
  }
  for (const mapping of value.mappings) {
    if (mapping.permission !== null &&
        (mapping.permission.customerId !== value.customerId || mapping.permission.sessionId !== value.sessionId)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["mappings", mapping.id, "permission"],
        message: "reference-source-permission-tenant-mismatch",
      });
    }
    if (value.bindings.sample.sourceKind === "external-style-only" &&
        mapping.sourceAssetId !== null && mapping.sourceDigest !== null &&
        isBoundReferenceSource(mapping.sourceAssetId, mapping.sourceDigest, value.bindings.sample)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["mappings", mapping.id, "sourceAssetId"],
        message: "external-reference-source-relabeling-forbidden",
      });
    }
    if (mapping.sourceProvenance === "self-owned-reference") {
      if (value.bindings.sample.sourceKind !== "self-owned" ||
          mapping.reuseRights?.id !== value.bindings.rights.id ||
          mapping.reuseRights?.version !== value.bindings.rights.version) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["mappings", mapping.id, "reuseRights"],
          message: "self-owned-reference-rights-binding-mismatch",
        });
      }
      if (mapping.reuseRights !== null &&
          !value.bindings.rights.reusableElements.includes(mapping.reuseRights.element)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["mappings", mapping.id, "reuseRights", "element"],
          message: "self-owned-reference-reuse-element-not-authorized",
        });
      }
    }
  }
  const expectedUnresolved = value.mappings.filter((mapping) => mapping.status === "unachievable").length;
  if (value.unresolvedCount !== expectedUnresolved) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["unresolvedCount"],
      message: "reference-mapping-unresolved-count-mismatch",
    });
  }
  const confirmationValues = [value.confirmedByActorId, value.confirmationReceiptId, value.confirmedAt];
  const hasAllConfirmation = confirmationValues.every((field) => field !== null);
  const hasAnyConfirmation = confirmationValues.some((field) => field !== null);
  if (value.status === "confirmed" && !hasAllConfirmation || value.status === "draft" && hasAnyConfirmation ||
      value.status === "invalidated" && hasAnyConfirmation && !hasAllConfirmation) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["confirmedAt"],
      message: "reference-mapping-confirmation-state-mismatch",
    });
  }
});

const ReferenceAuthorizedSourceSchema = z.object({
  id: NonEmptyIdSchema,
  mappingId: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  provenance: ReferenceSourceProvenanceSchema,
  permission: ReferencePermissionBindingSchema,
  reuseRights: ReferenceReuseRightsBindingSchema.nullable(),
}).strict().superRefine((value, ctx) => {
  validatePermissionBinding(value, ctx);
  if (value.provenance === "self-owned-reference" && value.reuseRights === null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["reuseRights"],
      message: "self-owned-reference-rights-required",
    });
  }
  if (value.provenance !== "self-owned-reference" && value.reuseRights !== null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["reuseRights"],
      message: "customer-source-reference-rights-forbidden",
    });
  }
});

const ExecutableTransitionSchema = z.enum(["cut", "fade", "crossfade"]);

const ReferenceTimelineEntrySchema = z.object({
  id: NonEmptyIdSchema,
  mappingId: NonEmptyIdSchema,
  ruleId: NonEmptyIdSchema,
  sourceBindingId: NonEmptyIdSchema,
  sourceStartMs: NonNegativeMillisecondsSchema,
  sourceEndMs: PositiveMillisecondsSchema,
  timelineStartMs: NonNegativeMillisecondsSchema,
  transition: ExecutableTransitionSchema,
  targetDurationRatio: z.number().positive(),
  reasonCode: SafeCodeSchema,
}).strict().superRefine((value, ctx) => {
  if (value.sourceEndMs <= value.sourceStartMs) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["sourceEndMs"],
      message: "source-end-must-follow-start",
    });
  }
});

const ReferenceAudioOverlaySchema = z.object({
  sourceBindingId: NonEmptyIdSchema,
  sourceStartMs: NonNegativeMillisecondsSchema,
  sourceEndMs: PositiveMillisecondsSchema,
  volume: z.number().min(0).max(1),
}).strict().superRefine((value, ctx) => {
  if (value.sourceEndMs <= value.sourceStartMs) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["sourceEndMs"],
      message: "source-end-must-follow-start",
    });
  }
});

export const ReferenceVariantPlanSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  bindings: RenderBindingsSchema,
  kind: ReferenceVariantKindSchema,
  authorizedSources: z.array(ReferenceAuthorizedSourceSchema).min(1),
  timeline: z.array(ReferenceTimelineEntrySchema).min(1),
  audioOverlay: ReferenceAudioOverlaySchema.nullable(),
  platformChangeCodes: z.array(SafeCodeSchema).refine(unique, "duplicate-reference-platform-change"),
  status: z.enum(["planned", "rendered", "invalidated"]),
  publishClaimed: z.literal(false),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  validateScopedBinding(value.customerId, value.sessionId, {
    sample: value.bindings.sample,
    rights: value.bindings.rights,
    analysis: value.bindings.analysis,
    styleSelection: value.bindings.styleSelection,
    mappingPlan: value.bindings.mappingPlan,
  }, ctx);
  if (!unique(value.timeline.map((entry) => entry.id)) ||
      !unique(value.authorizedSources.map((source) => source.id))) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["timeline"],
      message: "duplicate-reference-timeline-or-source-binding-id",
    });
  }
  const sources = new Map(value.authorizedSources.map((source) => [source.id, source]));
  const permissions = new Map(value.bindings.mappingPlan.permissions.map((permission) => [permission.id, permission]));
  for (const source of value.authorizedSources) {
    if (source.customerId !== value.customerId || source.sessionId !== value.sessionId ||
        source.permission.customerId !== value.customerId || source.permission.sessionId !== value.sessionId) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["authorizedSources", source.id],
        message: "reference-authorized-source-tenant-mismatch",
      });
    }
    if (value.bindings.sample.sourceKind === "external-style-only" &&
        isBoundReferenceSource(source.sourceAssetId, source.sourceDigest, value.bindings.sample)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["authorizedSources", source.id, "sourceAssetId"],
        message: "external-reference-source-relabeling-forbidden",
      });
    }
    if (source.provenance === "self-owned-reference" &&
        (value.bindings.sample.sourceKind !== "self-owned" ||
         source.reuseRights?.id !== value.bindings.rights.id ||
         source.reuseRights?.version !== value.bindings.rights.version)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["authorizedSources", source.id, "reuseRights"],
        message: "self-owned-reference-rights-binding-mismatch",
      });
    }
    if (source.provenance === "self-owned-reference" && source.reuseRights !== null &&
        !value.bindings.rights.reusableElements.includes(source.reuseRights.element)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["authorizedSources", source.id, "reuseRights", "element"],
        message: "self-owned-reference-reuse-element-not-authorized",
      });
    }
    const permission = permissions.get(source.permission.id);
    if (!permission || permission.version !== source.permission.version ||
        permission.epoch !== source.permission.epoch ||
        permission.customerId !== source.permission.customerId ||
        permission.sessionId !== source.permission.sessionId ||
        permission.sourceAssetId !== source.permission.sourceAssetId ||
        permission.sourceDigest !== source.permission.sourceDigest) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["authorizedSources", source.id, "permission"],
        message: "reference-source-permission-version-stale",
      });
    }
  }
  for (const entry of value.timeline) {
    const source = sources.get(entry.sourceBindingId);
    if (!source || source.mappingId !== entry.mappingId) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["timeline", entry.id, "sourceBindingId"],
        message: "reference-timeline-source-binding-mismatch",
      });
    }
    if (source?.provenance === "self-owned-reference" && source.reuseRights?.element !== "video-clips") {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["timeline", entry.id, "sourceBindingId"],
        message: "reference-video-reuse-rights-required",
      });
    }
  }
  if (value.audioOverlay !== null) {
    const audioSource = sources.get(value.audioOverlay.sourceBindingId);
    if (!audioSource) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["audioOverlay", "sourceBindingId"],
        message: "reference-audio-source-binding-mismatch",
      });
    }
    if (audioSource?.provenance === "self-owned-reference" && audioSource.reuseRights?.element !== "audio-track") {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["audioOverlay", "sourceBindingId"],
        message: "reference-audio-reuse-rights-required",
      });
    }
  }
  if (value.kind !== "editorial-master" && value.platformChangeCodes.length === 0) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["platformChangeCodes"],
      message: "reference-platform-variant-change-required",
    });
  }
});

const ReferenceDraftVariantBindingSchema = z.object({
  id: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  kind: ReferenceVariantKindSchema,
}).strict();

const ReferenceDraftLineageSchema = z.object({
  id: NonEmptyIdSchema,
  use: z.enum(["video", "audio-overlay"]),
  timelineEntryId: NonEmptyIdSchema.nullable(),
  mappingId: NonEmptyIdSchema,
  sourceBindingId: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceStartMs: NonNegativeMillisecondsSchema,
  sourceEndMs: PositiveMillisecondsSchema,
}).strict().superRefine((value, ctx) => {
  if (value.sourceEndMs <= value.sourceStartMs) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceEndMs"], message: "source-end-must-follow-start" });
  }
  if (value.use === "video" && value.timelineEntryId === null ||
      value.use === "audio-overlay" && value.timelineEntryId !== null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["timelineEntryId"],
      message: "reference-draft-lineage-use-mismatch",
    });
  }
});

export const ReferenceDraftPackageSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: z.literal(1),
  bindings: z.object({
    mappingPlan: ConfirmedMappingPlanBindingSchema,
    variantPlan: ReferenceDraftVariantBindingSchema,
  }).strict(),
  outputDigest: DigestSchema,
  outputPath: NonEmptyIdSchema,
  durationSeconds: z.number().min(30).max(60),
  lineage: z.array(ReferenceDraftLineageSchema).min(1),
  status: z.enum(["reviewable", "approved", "invalidated"]),
  capability: StudioCapabilitySchema,
  publishClaimed: z.literal(false),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (value.bindings.mappingPlan.customerId !== value.customerId ||
      value.bindings.mappingPlan.sessionId !== value.sessionId ||
      value.bindings.variantPlan.customerId !== value.customerId ||
      value.bindings.variantPlan.sessionId !== value.sessionId) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["bindings"],
      message: "reference-draft-binding-scope-mismatch",
    });
  }
  if (!unique(value.lineage.map((row) => row.id))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["lineage"], message: "duplicate-reference-lineage-id" });
  }
});

export const ReferenceDraftApprovalSchema = z.object({
  kind: z.literal("reference-draft"),
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: z.literal(1),
  bindings: z.object({
    executionReport: z.object({
      id: NonEmptyIdSchema,
      version: PositiveVersionSchema,
    }).strict(),
    draft: z.object({
      id: NonEmptyIdSchema,
      version: PositiveVersionSchema,
      outputDigest: DigestSchema,
    }).strict(),
    mappingPlan: z.object({
      id: NonEmptyIdSchema,
      version: PositiveVersionSchema,
      contentDigest: DigestSchema,
      permissionSetDigest: DigestSchema,
    }).strict(),
  }).strict(),
  variant: ReferenceVariantKindSchema,
  reviewerId: NonEmptyIdSchema,
  fileDigest: DigestSchema,
  approvedAt: DateTimeSchema,
  status: z.enum(["active", "invalidated"]),
  publishClaimed: z.literal(false),
  // Keep the reference approval structurally readable beside legacy approvals
  // while forbidding callers from inventing legacy cover/still evidence.
  storyId: z.never().optional(),
  revisionId: z.never().optional(),
  revision: z.never().optional(),
  artifactId: z.never().optional(),
  artifactDigest: z.never().optional(),
  coverDigest: z.never().optional(),
  stillDigests: z.never().optional(),
  renderInputDigest: z.never().optional(),
}).strict().superRefine((value, ctx) => {
  if (value.fileDigest !== value.bindings.draft.outputDigest) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["fileDigest"],
      message: "reference-approval-file-digest-mismatch",
    });
  }
});

const ReferenceExecutionSectionSchema = z.object({
  status: ReferenceMappingStatusSchema,
  summaryCode: SafeCodeSchema,
  evidenceRefs: z.array(SafeCodeSchema).refine(unique, "duplicate-reference-execution-evidence"),
}).strict();

const ReferenceMappingCountsSchema = z.object({
  matched: z.number().int().nonnegative(),
  substituted: z.number().int().nonnegative(),
  unachievable: z.number().int().nonnegative(),
}).strict();

const ReferencePlatformChangesSchema = z.object({
  kind: z.enum(["xiaohongshu", "douyin", "wechat-channels"]),
  changeCodes: z.array(SafeCodeSchema).min(1).refine(unique, "duplicate-reference-platform-change"),
}).strict();

const ReferenceArtifactBindingSchema = z.object({
  kind: ReferenceVariantKindSchema,
  variantPlanId: NonEmptyIdSchema,
  variantPlanVersion: PositiveVersionSchema,
  draftId: NonEmptyIdSchema,
  draftVersion: PositiveVersionSchema,
  outputDigest: DigestSchema,
}).strict();

const ReferenceExecutionOperationSchema = z.object({
  operationId: NonEmptyIdSchema,
  method: z.enum(["render", "reviseWithPrompt"]),
  inputBindingDigest: DigestSchema,
  target: ReferenceVariantKindSchema.nullable(),
  promptDigest: DigestSchema.nullable(),
  producedArtifacts: z.array(ReferenceArtifactBindingSchema).min(1),
}).strict();

function validateArtifactSet(
  artifacts: Array<z.infer<typeof ReferenceArtifactBindingSchema>>,
  ctx: z.RefinementCtx,
  path: (string | number)[],
  message: string,
) {
  const kinds = artifacts.map((artifact) => artifact.kind);
  if (!unique(kinds) || !ReferenceVariantKindSchema.options.every((kind) => kinds.includes(kind))) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path, message });
  }
}

export const ReferenceExecutionReportSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  bindings: RenderBindingsSchema,
  artifacts: z.array(ReferenceArtifactBindingSchema),
  rhythm: ReferenceExecutionSectionSchema,
  transitions: ReferenceExecutionSectionSchema,
  narrative: ReferenceExecutionSectionSchema,
  audio: ReferenceExecutionSectionSchema,
  mappingCounts: ReferenceMappingCountsSchema,
  platformChanges: z.array(ReferencePlatformChangesSchema),
  operation: ReferenceExecutionOperationSchema.optional(),
  status: z.enum(["reviewable", "invalidated"]),
  publishClaimed: z.literal(false),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  validateScopedBinding(value.customerId, value.sessionId, {
    sample: value.bindings.sample,
    rights: value.bindings.rights,
    analysis: value.bindings.analysis,
    styleSelection: value.bindings.styleSelection,
    mappingPlan: value.bindings.mappingPlan,
  }, ctx);
  validateArtifactSet(value.artifacts, ctx, ["artifacts"], "reference-variant-set-incomplete");
  const platformKinds = value.platformChanges.map((change) => change.kind);
  if (!unique(platformKinds) || platformKinds.length !== 3) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["platformChanges"],
      message: "reference-platform-change-set-incomplete",
    });
  }
  if (value.operation) {
    const producedKinds = value.operation.producedArtifacts.map((artifact) => artifact.kind);
    if (!unique(producedKinds)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["operation", "producedArtifacts"],
        message: "reference-operation-artifact-set-invalid",
      });
    }
    for (const produced of value.operation.producedArtifacts) {
      const artifact = value.artifacts.find((candidate) => candidate.kind === produced.kind);
      if (!artifact || JSON.stringify(artifact) !== JSON.stringify(produced)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["operation", "producedArtifacts", produced.kind],
          message: "reference-operation-artifact-binding-mismatch",
        });
      }
    }
    if (value.operation.method === "render") {
      if (value.operation.target !== null || value.operation.promptDigest !== null ||
          producedKinds.length !== 4 ||
          !ReferenceVariantKindSchema.options.every((kind) => producedKinds.includes(kind))) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["operation"],
          message: "reference-render-operation-metadata-invalid",
        });
      }
    } else {
      const target = value.operation.target;
      const expectedKinds = target === null ? []
        : target === "editorial-master" ? ReferenceVariantKindSchema.options
        : [target];
      if (target === null || value.operation.promptDigest === null ||
          producedKinds.length !== expectedKinds.length ||
          expectedKinds.some((kind) => !producedKinds.includes(kind))) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["operation"],
          message: "reference-revision-operation-metadata-invalid",
        });
      }
    }
  }
});

const ReferenceMobileArtifactSchema = ReferenceArtifactBindingSchema.extend({
  mediaRoute: z.object({
    kind: z.literal("reference-draft-media"),
    version: z.literal(1),
    draftId: NonEmptyIdSchema,
  }).strict(),
  status: z.enum(["reviewable", "approved", "invalidated"]),
}).strict().superRefine((value, ctx) => {
  if (value.mediaRoute.draftId !== value.draftId) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["mediaRoute", "draftId"],
      message: "reference-mobile-media-route-binding-mismatch",
    });
  }
});

const ReferenceMobileStyleSummarySchema = z.object({
  status: ReferenceAnalysisStatusSchema,
  ruleKinds: z.array(ReferenceRuleKindSchema).min(1).refine(unique, "duplicate-reference-rule-kind"),
  capability: z.discriminatedUnion("mode", [
    z.object({
      mode: z.literal("offline-fallback"),
      label: z.literal("离线/模拟分析"),
      version: z.literal(1),
      provider: z.null(),
    }).strict(),
    z.object({
      mode: z.literal("provider"),
      label: z.literal("移动审片：已配置模型服务 A/B，节奏/转场"),
      version: z.literal(1),
      provider: z.literal("configured-provider"),
    }).strict(),
  ]),
  warningCodes: z.array(SafeCodeSchema).refine(unique, "duplicate-reference-analysis-warning"),
}).strict();

const ReferenceMobileApprovalSchema = z.object({
  status: z.enum(["pending", "approved"]),
  approvedArtifactKinds: z.array(ReferenceVariantKindSchema)
    .refine(unique, "duplicate-reference-approved-artifact-kind"),
}).strict();

const ReferenceMobileExecutionReportSchema = z.object({
  id: NonEmptyIdSchema,
  version: PositiveVersionSchema,
  artifacts: z.array(ReferenceArtifactBindingSchema),
  rhythm: ReferenceExecutionSectionSchema,
  transitions: ReferenceExecutionSectionSchema,
  narrative: ReferenceExecutionSectionSchema,
  audio: ReferenceExecutionSectionSchema,
  mappingCounts: ReferenceMappingCountsSchema,
  platformChanges: z.array(ReferencePlatformChangesSchema),
}).strict().superRefine((value, ctx) => {
  validateArtifactSet(value.artifacts, ctx, ["artifacts"], "reference-variant-set-incomplete");
  const platformKinds = value.platformChanges.map((change) => change.kind);
  if (!unique(platformKinds) || platformKinds.length !== 3) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["platformChanges"],
      message: "reference-platform-change-set-incomplete",
    });
  }
});

export const ReferenceMobileReviewV1Schema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  version: z.literal(1),
  bindings: MobileBindingsSchema,
  styleSummary: ReferenceMobileStyleSummarySchema,
  artifacts: z.array(ReferenceMobileArtifactSchema),
  executionReport: ReferenceMobileExecutionReportSchema,
  approval: ReferenceMobileApprovalSchema,
  status: z.enum(["reviewable", "approved", "invalidated"]),
  publishClaimed: z.literal(false),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  validateScopedBinding(value.customerId, value.sessionId, {
    sample: value.bindings.sample,
    rights: value.bindings.rights,
    analysis: value.bindings.analysis,
    styleSelection: value.bindings.styleSelection,
    mappingPlan: value.bindings.mappingPlan,
    executionReport: value.bindings.executionReport,
  }, ctx);
  validateArtifactSet(value.artifacts, ctx, ["artifacts"], "reference-mobile-artifact-set-incomplete");
  if (value.artifacts.some((artifact, index) => artifact.kind !== ReferenceVariantKindSchema.options[index])) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["artifacts"],
      message: "reference-mobile-artifact-order-invalid",
    });
  }
  if (value.executionReport.id !== value.bindings.executionReport.id ||
      value.executionReport.version !== value.bindings.executionReport.version) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["executionReport"],
      message: "reference-mobile-report-binding-mismatch",
    });
  }
  const reportArtifacts = new Map(value.executionReport.artifacts.map((artifact) => [artifact.kind, artifact]));
  for (const artifact of value.artifacts) {
    const reportArtifact = reportArtifacts.get(artifact.kind);
    if (!reportArtifact ||
        reportArtifact.variantPlanId !== artifact.variantPlanId ||
        reportArtifact.variantPlanVersion !== artifact.variantPlanVersion ||
        reportArtifact.draftId !== artifact.draftId ||
        reportArtifact.draftVersion !== artifact.draftVersion ||
        reportArtifact.outputDigest !== artifact.outputDigest) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["artifacts", artifact.kind],
        message: "reference-mobile-artifact-report-binding-mismatch",
      });
    }
  }
  const approvedKinds = value.artifacts
    .filter((artifact) => artifact.status === "approved")
    .map((artifact) => artifact.kind);
  if (value.status !== "invalidated" &&
      JSON.stringify(value.approval.approvedArtifactKinds) !== JSON.stringify(approvedKinds)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["approval", "approvedArtifactKinds"],
      message: "reference-mobile-approval-artifact-mismatch",
    });
  }
  const fullyApproved = approvedKinds.length === ReferenceVariantKindSchema.options.length;
  if (value.status !== "invalidated" &&
      ((value.approval.status === "approved") !== fullyApproved ||
      (value.status === "approved") !== fullyApproved)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["approval", "status"],
      message: "reference-mobile-approval-status-mismatch",
    });
  }
  assertSafeMobileIdentities(value, ctx);
  assertNoUnsafeMobileValue(value, ctx);
});

const MOBILE_FORBIDDEN_KEYS = new Set([
  "sourcepath", "outputpath", "originalbytes", "externaltranscript",
  "externalcontent", "rawmodeloutput", "localpath", "filebytes",
]);
const MOBILE_SAFE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

function assertSafeMobileIdentities(
  value: unknown,
  ctx: z.RefinementCtx,
  path: (string | number)[] = [],
): void {
  if (Array.isArray(value)) {
    value.forEach((child, index) => assertSafeMobileIdentities(child, ctx, [...path, index]));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    const childPath = [...path, key];
    if (typeof child === "string" && (key === "id" || key.endsWith("Id")) &&
        !MOBILE_SAFE_ID_PATTERN.test(child)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: childPath,
        message: "reference-mobile-identity-invalid",
      });
      continue;
    }
    assertSafeMobileIdentities(child, ctx, childPath);
  }
}

function assertNoUnsafeMobileValue(value: unknown, ctx: z.RefinementCtx, path: (string | number)[] = []): void {
  if (typeof value === "string") {
    if (isLocalPathSyntax(value)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path,
        message: "reference-mobile-local-path-forbidden",
      });
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((child, index) => assertNoUnsafeMobileValue(child, ctx, [...path, index]));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    if (MOBILE_FORBIDDEN_KEYS.has(key.toLowerCase())) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: [...path, key],
        message: "reference-mobile-private-field-forbidden",
      });
      continue;
    }
    assertNoUnsafeMobileValue(child, ctx, [...path, key]);
  }
}

function isLocalPathSyntax(value: string): boolean {
  const raw = value.trim();
  const normalized = decodePercentLayers(raw);
  return /(?:^|[^A-Za-z0-9_])file:/i.test(normalized) ||
    /[A-Za-z]:[\\/]/.test(normalized) ||
    normalized.startsWith("/") ||
    normalized.includes("\\") ||
    /(?:^|[^\p{L}\p{N}_])\/(?=\S)/u.test(normalized) ||
    /\/[A-Za-z0-9._~-]+(?:\/[A-Za-z0-9._~-]+)+/.test(normalized);
}

function decodePercentLayers(value: string): string {
  let decoded = value;
  for (let pass = 0; pass <= value.length; pass += 1) {
    const next = decoded.replace(/(?:%[0-9a-f]{2})+/gi, (encoded) => {
      try {
        return decodeURIComponent(encoded);
      } catch {
        return encoded.replace(/%([0-9a-f]{2})/gi, (byte, hex: string) => {
          const code = Number.parseInt(hex, 16);
          return code <= 0x7f ? String.fromCharCode(code) : byte;
        });
      }
    });
    if (next === decoded) return decoded;
    decoded = next;
  }
  return decoded;
}

export type ReferenceSourceKind = z.infer<typeof ReferenceSourceKindSchema>;
export type ReferenceCollectionMode = z.infer<typeof ReferenceCollectionModeSchema>;
export type ReferenceReusableElement = z.infer<typeof ReferenceReusableElementSchema>;
export type ReferenceTransitionKind = z.infer<typeof ReferenceTransitionKindSchema>;
export type ReferenceMappingStatus = z.infer<typeof ReferenceMappingStatusSchema>;
export type ReferenceVariantKind = z.infer<typeof ReferenceVariantKindSchema>;
export type ReferenceAnalysisStatus = z.infer<typeof ReferenceAnalysisStatusSchema>;
export type ReferenceSourceProvenance = z.infer<typeof ReferenceSourceProvenanceSchema>;
export type ReferenceSample = z.infer<typeof ReferenceSampleSchema>;
export type ReferenceRightsDeclaration = z.infer<typeof ReferenceRightsDeclarationSchema>;
export type ReferenceStyleAnalysis = z.infer<typeof ReferenceStyleAnalysisSchema>;
export type ReferenceStyleSelection = z.infer<typeof ReferenceStyleSelectionSchema>;
export type ReferenceMaterialMappingPlan = z.infer<typeof ReferenceMaterialMappingPlanSchema>;
export type ReferenceDraftPackage = z.infer<typeof ReferenceDraftPackageSchema>;
export type ReferenceDraftApproval = z.infer<typeof ReferenceDraftApprovalSchema>;
export type ReferenceExecutionReport = z.infer<typeof ReferenceExecutionReportSchema>;
export type ReferenceVariantPlan = z.infer<typeof ReferenceVariantPlanSchema>;
export type ReferenceMobileReviewV1 = z.infer<typeof ReferenceMobileReviewV1Schema>;
