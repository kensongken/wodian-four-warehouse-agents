import { z } from "zod";

import { TimelineItemSchema, VideoVariantSchema } from "./content-editor";

const NonEmptyIdSchema = z.string().trim().min(1);
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const DateTimeSchema = z.string().datetime();
const EvidenceTimeSchema = z.number({
  required_error: "evidence-time-range-required",
  invalid_type_error: "evidence-time-range-required",
}).int().nonnegative();

export const EvidenceKindSchema = z.enum([
  "transcript-quote",
  "visible-person",
  "visible-product",
  "visible-action",
  "visible-scene",
]);

export const EvidenceCardSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  creativeSessionId: NonEmptyIdSchema,
  runId: NonEmptyIdSchema,
  sourceId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceStartMs: EvidenceTimeSchema,
  sourceEndMs: EvidenceTimeSchema,
  kind: EvidenceKindSchema,
  origin: z.enum(["new", "historical"]),
  value: z.string().trim().min(1),
  modelReceiptId: NonEmptyIdSchema,
  confidence: z.number().min(0).max(1),
  reviewStatus: z.enum(["pending", "accepted", "corrected", "rejected"]),
  reviewedBy: NonEmptyIdSchema.nullable(),
  reviewedAt: DateTimeSchema.nullable(),
  correctionReason: z.string().trim().min(1).nullable(),
}).strict().superRefine((value, ctx) => {
  if (value.sourceEndMs <= value.sourceStartMs) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "evidence-time-range-required",
      path: ["sourceEndMs"],
    });
  }
  const hasReviewMetadata = value.reviewedBy !== null && value.reviewedAt !== null;
  if (value.reviewStatus === "pending") {
    if (value.reviewedBy !== null || value.reviewedAt !== null || value.correctionReason !== null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "pending-evidence-cannot-have-review-metadata",
        path: ["reviewStatus"],
      });
    }
    return;
  }
  if (!hasReviewMetadata) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "reviewed-evidence-requires-review-metadata",
      path: ["reviewStatus"],
    });
  }
  if (
    (value.reviewStatus === "corrected" || value.reviewStatus === "rejected")
    && value.correctionReason === null
  ) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: value.reviewStatus === "corrected"
        ? "corrected-evidence-requires-reason"
        : "rejected-evidence-requires-reason",
      path: ["correctionReason"],
    });
  }
  if (value.reviewStatus === "accepted" && value.correctionReason !== null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "accepted-evidence-cannot-have-correction-reason",
      path: ["correctionReason"],
    });
  }
});

export const HumanFactDeclarationSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  creativeSessionId: NonEmptyIdSchema,
  kind: z.literal("opinion"),
  text: z.string().trim().min(1),
  declaredBy: NonEmptyIdSchema,
  confirmedBy: NonEmptyIdSchema,
  confirmedAt: DateTimeSchema,
  allowedUses: z.tuple([
    z.literal("persona-story"),
    z.literal("on-screen-text"),
  ]),
  voiceClaimed: z.literal(false, {
    errorMap: () => ({ message: "human-declaration-cannot-claim-voice" }),
  }),
  status: z.enum(["active", "withdrawn"]),
}).strict();

export const EvidenceDecisionSchema = z.object({
  id: NonEmptyIdSchema,
  cardId: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  creativeSessionId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  modelReceiptId: NonEmptyIdSchema,
  operationKey: NonEmptyIdSchema,
  action: z.enum(["accepted", "corrected", "rejected"]),
  correctedValue: z.string().trim().min(1).nullable(),
  correctionReason: z.string().trim().min(1).nullable(),
  reviewerId: NonEmptyIdSchema,
  reviewedAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (value.action === "accepted" && value.correctedValue !== null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "accepted-evidence-cannot-change-value",
      path: ["correctedValue"],
    });
  }
  if (value.action === "corrected" && (
    value.correctedValue === null || value.correctionReason === null
  )) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "corrected-evidence-requires-value-and-reason",
      path: ["action"],
    });
  }
  if (value.action === "rejected" && value.correctionReason === null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "rejected-evidence-requires-reason",
      path: ["correctionReason"],
    });
  }
  if (value.action !== "corrected" && value.correctedValue !== null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "only-corrected-evidence-may-change-value",
      path: ["correctedValue"],
    });
  }
});

export const RejectedEvidenceSuggestionSchema = z.object({
  id: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  creativeSessionId: NonEmptyIdSchema,
  runId: NonEmptyIdSchema,
  sourceId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourceStartMs: EvidenceTimeSchema,
  sourceEndMs: EvidenceTimeSchema,
  modelReceiptId: NonEmptyIdSchema,
  value: z.string().trim().min(1),
  rejectionCode: z.enum([
    "unsupported-identity-claim",
    "unsupported-material-claim",
    "unsupported-price-claim",
    "unsupported-ownership-claim",
    "unsupported-permission-claim",
    "unsupported-motive-claim",
    "unsupported-emotion-claim",
    "unsupported-untyped-visual-observation",
  ]),
  status: z.literal("rejected"),
  createdAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (value.sourceEndMs <= value.sourceStartMs) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "evidence-time-range-required",
      path: ["sourceEndMs"],
    });
  }
});

export const PersonaRevisionProposalSchema = z.object({
  id: NonEmptyIdSchema,
  creativeSessionId: NonEmptyIdSchema,
  storyId: NonEmptyIdSchema,
  baseRevisionId: NonEmptyIdSchema,
  prompt: z.string().trim().min(1),
  targetVariants: z.array(VideoVariantSchema).min(1),
  replacementTimelines: z.record(VideoVariantSchema, z.array(TimelineItemSchema)),
  explanation: z.string().trim().min(1),
  proposalDigest: DigestSchema,
  status: z.enum(["awaiting-confirmation", "confirmed", "rejected"]),
  createdAt: DateTimeSchema,
}).strict();

export type EvidenceKind = z.infer<typeof EvidenceKindSchema>;
export type EvidenceCard = z.infer<typeof EvidenceCardSchema>;
export type HumanFactDeclaration = z.infer<typeof HumanFactDeclarationSchema>;
export type EvidenceDecision = z.infer<typeof EvidenceDecisionSchema>;
export type RejectedEvidenceSuggestion = z.infer<typeof RejectedEvidenceSuggestionSchema>;
export type PersonaRevisionProposal = z.infer<typeof PersonaRevisionProposalSchema>;
