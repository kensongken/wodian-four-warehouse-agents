import { z } from "zod";
import { ContentSegmentSchema, EditRevisionSchema, StoryCandidateSchema, VariantArtifactSchema } from "./content-editor";

export const CreativeSessionStageSchema = z.enum([
  "draft_intent",
  "awaiting_material_consent",
  "reviewing_materials",
  "materials_declined",
  "awaiting_script_confirmation",
  "ready_to_dispatch",
  "shooting",
  "ingesting",
  "editorial_review",
  "completed",
  "paused",
  "cancelled",
  "invalidated",
]);

export const CreativeVersionKeySchema = z.object({
  sessionId: z.string().min(1),
  briefVersion: z.number().int().positive(),
  scriptVersion: z.number().int().positive(),
  shotPlanVersion: z.number().int().positive(),
});

export const ScriptProvenanceSchema = z.enum([
  "customer-input",
  "historical-material",
  "agent-suggestion",
  "shoot-suggestion",
]);

export const CreativeTaskStatusSchema = z.enum(["active", "invalidated"]);

export const DispatchTransportSchema = z.enum(["simulated"]);

export const DeviceRoleSchema = z.enum([
  "watch-director",
  "main-stone-a",
  "main-stone-b",
]);

export const CreativeSessionActionSchema = z.enum([
  "received",
  "started",
  "completed",
  "retake",
  "skipped",
  "voice-rewrite",
  "note",
  "privacy-paused",
  "disconnected",
  "media-returned",
]);

export const CreativeHandoffStatusSchema = z.enum([
  "collecting",
  "ready-for-editorial",
  "processing",
  "reviewable",
  "no-assets",
  "failed-retryable",
  "failed-manual-review",
]);

export const HistoricalMaterialAuthorizationSchema = z.object({
  creativeSessionId: z.string().min(1),
  suggestionSetId: z.string().min(1),
  suggestionSetVersion: z.number().int().positive(),
  decisionId: z.string().min(1),
  selectedSegmentIds: z.array(z.string().min(1)).refine(
    (ids) => new Set(ids).size === ids.length, "duplicate-authorized-segment-id",
  ),
}).strict();

export type HistoricalMaterialAuthorization = Readonly<
  Omit<z.infer<typeof HistoricalMaterialAuthorizationSchema>, "selectedSegmentIds"> & {
    selectedSegmentIds: readonly string[];
  }
>;
export type HistoricalMaterialConsent = HistoricalMaterialAuthorization | Readonly<{ selectedSegmentIds: readonly [] }>;

export const CreativeIntentAnswerOperationSchema = z.object({
  id: z.string().min(1),
  operationKey: z.string().min(1),
  route: z.string().min(1),
  bodyDigest: z.string().min(1),
  customerId: z.string().min(1),
  clientInstanceId: z.string().min(1),
  creativeSessionId: z.string().min(1),
  intentVersion: z.number().int().positive().nullable(),
  suggestionSetId: z.string().min(1).nullable(),
  phase: z.enum(["answer-pending", "answer-stored", "suggestion-stored", "complete"]),
  updatedAt: z.string().datetime(),
}).strict();

export type CreativeIntentAnswerOperation = z.infer<
  typeof CreativeIntentAnswerOperationSchema
>;

export const CreativeProcessingTaskSchema = z.object({
  id: z.string().min(1),
  creativeSessionId: z.string().min(1),
  editorialBatchId: z.string().min(1),
  status: CreativeHandoffStatusSchema,
  phase: z.enum(["capture", "segment", "tag", "story", "revision", "render", "complete"]),
  errorCode: z.string().min(1).nullable(),
  updatedAt: z.string().datetime(),
  operationKey: z.string().min(1).optional(),
  inputDigest: z.string().min(1).optional(),
  publishedClaimed: z.literal(false).optional(),
  supersededByTaskId: z.string().min(1).optional(),
  checkpoint: z.object({
    authorization: z.union([HistoricalMaterialAuthorizationSchema,
      z.object({ selectedSegmentIds: z.tuple([]) }).strict()]),
    assetIds: z.array(z.string().min(1)),
    segmentIds: z.array(z.string().min(1)),
    segmentDigest: z.string().min(1).optional(),
    historicalCandidates: z.array(ContentSegmentSchema).optional(),
    historicalCandidateDigest: z.string().min(1).optional(),
    historicalEvidenceDigest: z.string().min(1).optional(),
    stories: z.array(StoryCandidateSchema),
    revisions: z.array(EditRevisionSchema),
    artifacts: z.array(VariantArtifactSchema),
  }).optional(),
}).superRefine((task, ctx) => {
  if (task.status !== "reviewable") return;
  const checkpoint = task.checkpoint;
  if (task.phase !== "complete" || task.errorCode !== null || task.publishedClaimed !== false ||
    !task.operationKey || !task.inputDigest || !checkpoint || checkpoint.stories.length === 0 ||
    checkpoint.revisions.length !== checkpoint.stories.length || checkpoint.artifacts.length !== checkpoint.revisions.length * 4) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, message: "reviewable-output-checkpoint-required" });
  }
});

export const CreativeEditorialRestartAuthorizationSchema = z.object({
  id: z.string().min(1),
  creativeSessionId: z.string().min(1),
  failedTaskId: z.string().min(1),
  resetOperationKey: z.string().min(1),
  processOperationKey: z.string().min(1),
  confirmedBy: z.string().min(1),
  confirmationCode: z.literal("CONFIRM_MANUAL_EDITORIAL_RESTART"),
  confirmationText: z.string().min(1),
  confirmedAt: z.string().datetime(),
  status: z.enum(["authorized", "consumed"]),
  restartedTaskId: z.string().min(1).nullable(),
  consumedAt: z.string().datetime().nullable(),
}).strict();

export type CreativeEditorialRestartAuthorization = z.infer<
  typeof CreativeEditorialRestartAuthorizationSchema
>;

export const MaterialUseDecisionSchema = z
  .object({
    id: z.string().min(1),
    sessionId: z.string().min(1),
    suggestionSetId: z.string().min(1),
    suggestionVersion: z.number().int().positive(),
    action: z.enum(["decline-view", "accept-view", "confirm-selection"]),
    shownSegmentIds: z.array(z.string().min(1)),
    selectedSegmentIds: z.array(z.string().min(1)),
    rejectedSegmentIds: z.array(z.string().min(1)),
    actorId: z.string().min(1),
    createdAt: z.string().datetime(),
    purpose: z.enum(["pre-shoot-script", "post-shoot-edit"]).optional(),
  })
  .superRefine((value, ctx) => {
    const decisionSegmentLists = [
      value.shownSegmentIds,
      value.selectedSegmentIds,
      value.rejectedSegmentIds,
    ];
    if (decisionSegmentLists.some(hasDuplicateSegmentId)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "duplicate-decision-segment-id",
      });
    }
    if (value.selectedSegmentIds.some((id) => value.rejectedSegmentIds.includes(id))) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "selected-and-rejected-segment-overlap",
      });
    }
    const shown = new Set(value.shownSegmentIds);
    const invalid = [...value.selectedSegmentIds, ...value.rejectedSegmentIds].some(
      (id) => !shown.has(id),
    );
    if (invalid) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "decision-segment-not-shown",
      });
    }
    if (value.action !== "confirm-selection" && value.selectedSegmentIds.length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "selection-not-confirmed",
      });
    }
  });

export const CreativeSessionSchema = z.object({
  id: z.string().min(1),
  customerId: z.string().min(1),
  title: z.string().min(1),
  stage: CreativeSessionStageSchema,
  currentIntentVersion: z.number().int().nonnegative(),
  currentBriefVersion: z.number().int().nonnegative(),
  currentScriptVersion: z.number().int().nonnegative(),
  currentShotPlanVersion: z.number().int().nonnegative(),
  materialViewDecisionIds: z.array(z.string().min(1)),
  materialUseDecisionIds: z.array(z.string().min(1)),
  deviceBindings: z.array(
    z.object({
      deviceId: z.string().min(1),
      deviceRole: DeviceRoleSchema,
      transport: DispatchTransportSchema,
    }),
  ),
  captureBatchIds: z.array(z.string().min(1)),
  editorialBatchId: z.string().min(1).nullable(),
  privacyMode: z.enum(["creator", "visitor", "paused"]),
  prohibitedAreas: z.array(z.string().min(1)),
  status: CreativeTaskStatusSchema,
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
});

export const CreativeIntentSchema = z
  .object({
    id: z.string().min(1),
    sessionId: z.string().min(1),
    version: z.number().int().positive(),
    // Draft intents are deliberately persistable while the Agent is still
    // asking the customer which platform to target. Completeness is enforced
    // by the session transition gate, not by the storage contract.
    platforms: z.array(z.string().min(1)),
    audience: z.string().min(1),
    theme: z.string().min(1),
    products: z.array(
      z.object({
        id: z.string().min(1),
        status: z.enum(["suggested", "confirmed"]),
      }),
    ),
    shootConditions: z.array(z.string().min(1)),
    prohibitedAreas: z.array(z.string().min(1)),
    tone: z.string().min(1),
    durationSeconds: z.number().int().positive(),
    createdAt: z.string().datetime(),
  })
  .superRefine((value, ctx) => {
    if (new Set(value.products.map((product) => product.status)).size > 1) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "product-identity-status-must-not-mix",
      });
    }
  });

export const MaterialSuggestionSetSchema = z.object({
  id: z.string().min(1),
  sessionId: z.string().min(1),
  version: z.number().int().positive(),
  suggestions: z.array(
    z.object({
      segmentId: z.string().min(1),
      thumbnailPath: z.string().min(1),
      capturedAt: z.string().datetime(),
      productId: z.string().min(1).nullable(),
      processStage: z.string().min(1).nullable(),
      reason: z.string().min(1),
      source: z.string().min(1),
      permissionState: z.enum([
        "allowed",
        "review-required",
        "blocked",
        "withdrawn",
      ]),
      lastUsedAt: z.string().datetime().nullable(),
      technicallyAvailable: z.boolean(),
    }),
  ),
  createdAt: z.string().datetime(),
  purpose: z.enum(["pre-shoot-script", "post-shoot-edit"]).optional(),
});

export const CreativeBriefSchema = z.object({
  id: z.string().min(1),
  sessionId: z.string().min(1),
  intentVersion: z.number().int().positive(),
  briefVersion: z.number().int().positive(),
  title: z.string().min(1),
  platformDirection: z.string().min(1),
  audience: z.string().min(1),
  theme: z.string().min(1),
  tone: z.string().min(1),
  durationSeconds: z.number().int().positive(),
  confirmedSegmentIds: z.array(z.string().min(1)),
  status: CreativeTaskStatusSchema.default("active"),
  createdAt: z.string().datetime(),
});

export const ScriptPackageSchema = z.object({
  id: z.string().min(1),
  sessionId: z.string().min(1),
  briefVersion: z.number().int().positive(),
  scriptVersion: z.number().int().positive(),
  parentScriptId: z.string().min(1).nullable().optional(),
  designerPrompt: z.string().min(1).nullable().optional(),
  changeExplanation: z.string().min(1).nullable().optional(),
  status: CreativeTaskStatusSchema,
  title: z.string().min(1),
  platformDirection: z.string().min(1),
  opening: z.string().min(1),
  narrativeOrder: z.array(z.string().min(1)).min(1),
  sections: z.array(
    z.object({
      id: z.string().min(1),
      expression: z.string().min(1),
      source: ScriptProvenanceSchema,
      referencedSegmentIds: z.array(z.string().min(1)),
    }),
  ),
  confirmedAt: z.string().datetime().nullable(),
  createdAt: z.string().datetime(),
});

const OptionalGapSuggestionSchema = z.preprocess(
  (value) => typeof value === "string" ? { suggestion: value, required: false } : value,
  z.object({
    suggestion: z.string().min(1),
    required: z.literal(false),
  }),
);

export const ShotPlanSchema = CreativeVersionKeySchema.extend({
  id: z.string().min(1),
  status: CreativeTaskStatusSchema,
  steps: z.array(
    z.object({
      id: z.string().min(1),
      order: z.number().int().positive(),
      purpose: z.string().min(1),
      expressionSuggestion: z.string().min(1),
      visualDirection: z.string().min(1),
      durationSeconds: z.number().int().positive(),
      mustCapture: z.array(z.string().min(1)),
      optionalGapSuggestions: z.array(OptionalGapSuggestionSchema),
      prohibitedCapture: z.array(z.string().min(1)),
      completionCriteria: z.array(z.string().min(1)),
      deviceRoles: z.array(DeviceRoleSchema).min(1),
    }),
  ),
  createdAt: z.string().datetime(),
});

export const DirectorCardSchema = CreativeVersionKeySchema.extend({
  id: z.string().min(1),
  stepId: z.string().min(1),
  status: CreativeTaskStatusSchema,
  purpose: z.string().min(1),
  spokenPrompt: z.string().min(1),
  shotDescription: z.string().min(1),
  durationSeconds: z.number().int().positive(),
  mustCapture: z.array(z.string().min(1)),
  prohibitedCapture: z.array(z.string().min(1)),
  availableActions: z.array(CreativeSessionActionSchema).min(1),
  createdAt: z.string().datetime(),
});

export const DeviceBriefSchema = CreativeVersionKeySchema.extend({
  id: z.string().min(1),
  stepId: z.string().min(1),
  status: CreativeTaskStatusSchema,
  deviceId: z.string().min(1),
  deviceRole: DeviceRoleSchema,
  carrier: z.string().min(1),
  startCondition: z.string().min(1),
  endCondition: z.string().min(1),
  composition: z.string().min(1),
  prohibitedAreas: z.array(z.string().min(1)),
  privacyState: z.enum(["creator", "visitor", "paused"]),
  createdAt: z.string().datetime(),
});

export const DispatchReceiptSchema = CreativeVersionKeySchema.extend({
  id: z.string().min(1),
  deviceId: z.string().min(1),
  deviceRole: DeviceRoleSchema,
  transport: DispatchTransportSchema,
  idempotencyKey: z.string().min(1),
  status: z.enum(["pending", "received", "failed"]),
  createdAt: z.string().datetime(),
});

export const CreativeSessionEventSchema = CreativeVersionKeySchema.extend({
  id: z.string().min(1),
  stepId: z.string().min(1).nullable(),
  deviceId: z.string().min(1).nullable(),
  deviceRole: DeviceRoleSchema.nullable(),
  action: CreativeSessionActionSchema,
  actorId: z.string().min(1).nullable(),
  reason: z.string().min(1).nullable(),
  transcript: z.string().nullable().optional(),
  idempotencyKey: z.string().min(1),
  take: z.number().int().positive().optional(),
  createdAt: z.string().datetime(),
}).superRefine((value, ctx) => {
  if (value.action === "voice-rewrite" && (typeof value.transcript !== "string" ||
    value.transcript.trim().length === 0)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, message: "voice-rewrite-transcript-required" });
  }
  if (value.action !== "voice-rewrite" && value.transcript !== undefined &&
    value.transcript !== null) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, message: "unexpected-action-transcript" });
  }
});

export type CreativeSessionStage = z.infer<typeof CreativeSessionStageSchema>;
export type CreativeVersionKey = z.infer<typeof CreativeVersionKeySchema>;
export type ScriptProvenance = z.infer<typeof ScriptProvenanceSchema>;
export type CreativeTaskStatus = z.infer<typeof CreativeTaskStatusSchema>;
export type DispatchTransport = z.infer<typeof DispatchTransportSchema>;
export type DeviceRole = z.infer<typeof DeviceRoleSchema>;
export type CreativeSessionAction = z.infer<typeof CreativeSessionActionSchema>;
export type CreativeHandoffStatus = z.infer<typeof CreativeHandoffStatusSchema>;
export type CreativeProcessingTask = z.infer<typeof CreativeProcessingTaskSchema>;
export type CreativeSession = z.infer<typeof CreativeSessionSchema>;
export type CreativeIntent = z.infer<typeof CreativeIntentSchema>;
export type MaterialSuggestionSet = z.infer<typeof MaterialSuggestionSetSchema>;
export type MaterialUseDecision = z.infer<typeof MaterialUseDecisionSchema>;
export type CreativeBrief = z.infer<typeof CreativeBriefSchema>;
export type ScriptPackage = z.infer<typeof ScriptPackageSchema>;
export type ShotPlan = z.infer<typeof ShotPlanSchema>;
export type DirectorCard = z.infer<typeof DirectorCardSchema>;
export type DeviceBrief = z.infer<typeof DeviceBriefSchema>;
export type DispatchReceipt = z.infer<typeof DispatchReceiptSchema>;
export type CreativeSessionEvent = z.infer<typeof CreativeSessionEventSchema>;

function hasDuplicateSegmentId(segmentIds: string[]): boolean {
  return new Set(segmentIds).size !== segmentIds.length;
}
