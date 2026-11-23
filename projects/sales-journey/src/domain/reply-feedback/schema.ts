import { z } from "zod";
import { SALES_SCENARIOS, SALES_STAGES } from "@/domain/decision-card/types";
import { PRIVACY_REASON_CODES } from "@/domain/reply-feedback/redaction";

const NonBlank = z.string().trim().min(1);

/** Server-owned release identity, signed into every decision-usage proof. */
export const REPLY_AGENT_VERSION = "reply-agent.v2";

export const REPLY_FEEDBACK_REASONS = [
  "too_stiff",
  "too_long",
  "wrong_stage",
  "fact_inaccurate",
  "no_next_step",
  "other",
] as const;

export const ReplyFeedbackReasonSchema = z.enum(REPLY_FEEDBACK_REASONS);
export const PrivacyReasonCodeSchema = z.enum(PRIVACY_REASON_CODES);
export const ReplyFeedbackSaveOutcomeSchema = z.enum(["created", "updated", "unchanged"]);
const ReplyFeedbackRatingSchema = z.enum(["helpful", "needs_improvement"]);
const ReplyFeedbackReasonCodesSchema = z.array(ReplyFeedbackReasonSchema).max(6).refine(
  (reasons) => new Set(reasons).size === reasons.length,
  { message: "改进原因不能重复。" },
);

const ReplyFeedbackMetadataSchema = z.object({
  tenantId: NonBlank,
  operatorId: NonBlank,
  cardId: NonBlank,
  agentVersion: NonBlank,
  rating: ReplyFeedbackRatingSchema,
  reasons: ReplyFeedbackReasonCodesSchema,
  scenario: z.enum(SALES_SCENARIOS),
  salesStage: z.enum(SALES_STAGES),
  evidenceIds: z.array(NonBlank).max(20),
});

function requireImprovementReason(
  value: { rating: "helpful" | "needs_improvement"; reasons: readonly string[] },
  context: z.RefinementCtx,
): void {
  if (value.rating === "needs_improvement" && value.reasons.length === 0) {
    context.addIssue({
      code: "custom",
      path: ["reasons"],
      message: "请选择改进原因。",
    });
  }
}

export const ReplyFeedbackRequestSchema = ReplyFeedbackMetadataSchema.extend({
  finalReply: z.string().trim().max(1000).optional(),
}).strict().superRefine(requireImprovementReason);

const StoredReplyFeedbackMetadataSchema = ReplyFeedbackMetadataSchema.extend({
  feedbackId: NonBlank,
  createdAt: z.string().datetime(),
});

const SafeStoredReplyFeedbackSchema = StoredReplyFeedbackMetadataSchema.extend({
  privacyStatus: z.literal("safe"),
  privacyReasonCodes: z.array(PrivacyReasonCodeSchema).length(0),
  finalReply: z.string().max(1000).optional(),
}).strict();

const QuarantinedStoredReplyFeedbackSchema = StoredReplyFeedbackMetadataSchema.extend({
  privacyStatus: z.literal("quarantined"),
  privacyReasonCodes: z.array(PrivacyReasonCodeSchema).min(1),
}).strict();

export const StoredReplyFeedbackSchema = z.discriminatedUnion("privacyStatus", [
  SafeStoredReplyFeedbackSchema,
  QuarantinedStoredReplyFeedbackSchema,
]).superRefine(requireImprovementReason);

const AcceptedReplyFeedbackBaseSchema = z.object({
  accepted: z.literal(true),
  outcome: ReplyFeedbackSaveOutcomeSchema,
});

export const ReplyFeedbackAcceptedResponseSchema = z.discriminatedUnion("privacyStatus", [
  AcceptedReplyFeedbackBaseSchema.extend({
    privacyStatus: z.literal("safe"),
    privacyReasonCodes: z.array(PrivacyReasonCodeSchema).length(0),
  }).strict(),
  AcceptedReplyFeedbackBaseSchema.extend({
    privacyStatus: z.literal("quarantined"),
    privacyReasonCodes: z.array(PrivacyReasonCodeSchema).min(1),
  }).strict(),
]);

/**
 * Browser input deliberately contains only the evaluation and the opaque card
 * proof. Tenant, employee, customer, card context, stage, and evidence are
 * derived by the supervision service after it verifies that proof.
 */
export const EmployeeReplyFeedbackRequestSchema = z.object({
  usageToken: NonBlank.max(4_000),
  rating: ReplyFeedbackRatingSchema,
  reasonCodes: ReplyFeedbackReasonCodesSchema,
  finalReply: z.string().trim().max(1_000).optional(),
}).strict().superRefine((value, context) => {
  requireImprovementReason({ rating: value.rating, reasons: value.reasonCodes }, context);
});

const SupervisionReplyFeedbackContextSchema = z.object({
  tenantId: NonBlank.max(120),
  employeeUserId: NonBlank.max(120),
  customerId: NonBlank.max(120),
  decisionCardId: NonBlank.max(120),
  cardInstanceId: NonBlank.max(120),
  cardDigest: z.string().regex(/^sha256_[a-f0-9]{64}$/u),
  agentVersion: NonBlank.max(120),
  rating: ReplyFeedbackRatingSchema,
  reasonCodes: ReplyFeedbackReasonCodesSchema,
  scenario: z.enum(SALES_SCENARIOS),
  salesStage: z.enum(SALES_STAGES),
  evidenceIds: z.array(NonBlank.max(120)).max(20),
});

const CreateReplyFeedbackBaseSchema = SupervisionReplyFeedbackContextSchema.extend({
  privacyReasonCodes: z.array(PrivacyReasonCodeSchema),
});

const SafeCreateReplyFeedbackInputSchema = CreateReplyFeedbackBaseSchema.extend({
  privacyStatus: z.literal("safe"),
  privacyReasonCodes: z.array(PrivacyReasonCodeSchema).length(0),
  sanitizedFinalReply: z.string().max(1_000).optional(),
}).strict();

const QuarantinedCreateReplyFeedbackInputSchema = CreateReplyFeedbackBaseSchema.extend({
  privacyStatus: z.literal("quarantined"),
  privacyReasonCodes: z.array(PrivacyReasonCodeSchema).min(1),
}).strict();

/** Parsed server-derived input for repository writes. */
export const CreateReplyFeedbackInputSchema = z.discriminatedUnion("privacyStatus", [
  SafeCreateReplyFeedbackInputSchema,
  QuarantinedCreateReplyFeedbackInputSchema,
]).superRefine((value, context) => {
  requireImprovementReason({ rating: value.rating, reasons: value.reasonCodes }, context);
});

const ReplyFeedbackRecordBaseSchema = CreateReplyFeedbackBaseSchema.extend({
  id: NonBlank.max(120),
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
});

const SafeReplyFeedbackRecordSchema = ReplyFeedbackRecordBaseSchema.extend({
  privacyStatus: z.literal("safe"),
  privacyReasonCodes: z.array(PrivacyReasonCodeSchema).length(0),
  sanitizedFinalReply: z.string().max(1_000).optional(),
}).strict();

const QuarantinedReplyFeedbackRecordSchema = ReplyFeedbackRecordBaseSchema.extend({
  privacyStatus: z.literal("quarantined"),
  privacyReasonCodes: z.array(PrivacyReasonCodeSchema).min(1),
}).strict();

/**
 * Durable feedback rows are a discriminated union so quarantined rows cannot
 * carry a final-reply field, even as an empty or placeholder value.
 */
export const ReplyFeedbackRecordSchema = z.discriminatedUnion("privacyStatus", [
  SafeReplyFeedbackRecordSchema,
  QuarantinedReplyFeedbackRecordSchema,
]).superRefine((value, context) => {
  requireImprovementReason({ rating: value.rating, reasons: value.reasonCodes }, context);
});

export type ReplyFeedbackReason = typeof REPLY_FEEDBACK_REASONS[number];
export type ReplyFeedbackRequest = z.infer<typeof ReplyFeedbackRequestSchema>;
export type StoredReplyFeedback = z.infer<typeof StoredReplyFeedbackSchema>;
export type ReplyFeedbackAcceptedResponse = z.infer<typeof ReplyFeedbackAcceptedResponseSchema>;
export type EmployeeReplyFeedbackRequest = z.infer<typeof EmployeeReplyFeedbackRequestSchema>;
export type CreateReplyFeedbackInputRecord = z.infer<typeof CreateReplyFeedbackInputSchema>;
export type ReplyFeedbackRecordInput = z.infer<typeof ReplyFeedbackRecordSchema>;
