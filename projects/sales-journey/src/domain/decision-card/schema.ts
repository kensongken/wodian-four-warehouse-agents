import { z } from "zod";

import {
  CUSTOMER_INTENTS,
  SAFETY_RULE_IDS,
  SALES_SCENARIOS,
  SALES_STAGES,
  VERIFICATION_FIELDS,
} from "./types";

const NonBlankStringSchema = z.string().trim().min(1);

export const ConversationTurnSchema = z.object({
  speaker: z.enum(["customer", "operator"]),
  text: NonBlankStringSchema.max(1000),
  occurredAt: z.string().datetime().optional(),
});

export const EvidenceRefSchema = z.object({
  id: NonBlankStringSchema,
  kind: z.enum(["product_record", "certificate", "service_policy", "approved_copy"]),
  title: NonBlankStringSchema,
  verifiedFields: z.array(
    z.enum([
      "identity_bead",
      "scan_service",
      "material",
      "quality",
      "origin",
      "certificate",
      "reuse_or_return_process",
      "after_sales_policy",
      "price_or_payment",
    ]),
  ),
  status: z.enum(["approved", "expired", "withdrawn"]),
});

export const DecisionCardRequestSchema = z.object({
  contractVersion: z.literal("decision-card.v1"),
  requestId: NonBlankStringSchema,
  tenantId: NonBlankStringSchema,
  operatorId: NonBlankStringSchema,
  scenario: z.enum(SALES_SCENARIOS),
  productDescription: NonBlankStringSchema,
  latestCustomerMessage: NonBlankStringSchema,
  conversation: z.array(ConversationTurnSchema).max(12),
  operatorStyle: z
    .object({
      tone: z.enum(["professional", "warm", "direct"]),
      maxReplyCharacters: z.number().int().positive(),
      preferredGreeting: NonBlankStringSchema.optional(),
    })
    .optional(),
  evidence: z.array(EvidenceRefSchema),
});

export const ReplyOptionSchema = z.object({
  id: NonBlankStringSchema,
  strategy: z.enum([
    "explain",
    "clarify",
    "offer_service_path",
    "collect_review_material",
  ]),
  text: NonBlankStringSchema,
  evidenceIds: z.array(NonBlankStringSchema),
});

export const ReviewFocusSchema = z.object({
  field: z.enum(VERIFICATION_FIELDS),
  operatorPrompt: NonBlankStringSchema,
});

export const ProductStrategySealSchema = z.object({
  productCode: z.string().min(1).max(64).regex(/^[A-Za-z0-9_-]+$/u),
  strategyInputVersion: z.number().int().positive(),
  contentSha256: z.string().regex(/^[a-f0-9]{64}$/u),
  status: z.literal("preview_only"),
}).strict();

export const DecisionCardSchema = z
  .object({
    contractVersion: z.literal("decision-card.v1"),
    cardId: NonBlankStringSchema,
    requestId: NonBlankStringSchema,
    deliveryMode: z.literal("copy_only"),
    scenario: z.enum(SALES_SCENARIOS),
    customerIntent: z.enum(CUSTOMER_INTENTS),
    salesStage: z.enum(SALES_STAGES),
    summary: NonBlankStringSchema,
    replyOptions: z.array(ReplyOptionSchema).min(1).max(3),
    nextAction: z.object({
      kind: z.enum([
        "ask_clarifying_question",
        "share_verified_service_path",
        "collect_review_material",
        "handoff_to_operator",
      ]),
      text: NonBlankStringSchema,
    }),
    verificationNeeded: z.array(
      z.object({
        field: z.enum(VERIFICATION_FIELDS),
        reason: NonBlankStringSchema,
      }),
    ),
    reviewFocus: ReviewFocusSchema.optional(),
    safety: z.object({
      status: z.enum(["safe", "reviewRequired"]),
      rules: z.array(z.enum(SAFETY_RULE_IDS)),
      reasons: z.array(NonBlankStringSchema),
    }),
    audit: z.object({
      tenantId: NonBlankStringSchema,
      operatorId: NonBlankStringSchema,
      evidenceIds: z.array(NonBlankStringSchema),
    }),
    productStrategySeal: ProductStrategySealSchema.optional(),
  })
  .superRefine((card, context) => {
    if (card.reviewFocus === undefined) {
      return;
    }

    if (card.safety.status !== "reviewRequired") {
      context.addIssue({
        code: "custom",
        message: "仅人工复核卡可包含优先核对点。",
        path: ["reviewFocus"],
      });
    }

    if (
      !card.verificationNeeded.some(
        (verification) => verification.field === card.reviewFocus?.field,
      )
    ) {
      context.addIssue({
        code: "custom",
        message: "优先核对点必须来自核验清单。",
        path: ["reviewFocus", "field"],
      });
    }
  });

export type ConversationTurn = z.infer<typeof ConversationTurnSchema>;
export type EvidenceRef = z.infer<typeof EvidenceRefSchema>;
export type DecisionCardRequest = z.infer<typeof DecisionCardRequestSchema>;
export type ReplyOption = z.infer<typeof ReplyOptionSchema>;
export type ProductStrategySeal = z.infer<typeof ProductStrategySealSchema>;
export type DecisionCard = z.infer<typeof DecisionCardSchema>;
