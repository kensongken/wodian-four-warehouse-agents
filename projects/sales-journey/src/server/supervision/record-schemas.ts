import { z } from "zod";

import { JEWELRY_CATEGORIES } from "@/domain/decision-card/local-evidence";
import { SAFETY_RULE_IDS, SALES_SCENARIOS } from "@/domain/decision-card/types";
import {
  VaultAssetRecordSchema,
  VaultPendingUploadRecordSchema,
} from "@/domain/supervision/schemas";

const NonBlankStringSchema = z.string().trim().min(1);
const OptionalNonBlankStringSchema = NonBlankStringSchema.optional();
const IsoTimestampSchema = z.string().datetime();
const IdempotencyKeySchema = z.string().regex(/^idem_[a-f0-9]{64}$/u).optional();
const RequestFingerprintSchema = z.string().regex(/^fp_[a-f0-9]{64}$/u).optional();
const PersistedRecordSchema = z.object({
  id: NonBlankStringSchema,
  tenantId: NonBlankStringSchema,
  version: z.number().int().positive(),
  createdAt: IsoTimestampSchema,
});

export const CustomerRecordSchema = PersistedRecordSchema.extend({
  employeeUserId: NonBlankStringSchema,
  idempotencyKey: IdempotencyKeySchema,
  requestFingerprint: RequestFingerprintSchema,
  customerFingerprint: RequestFingerprintSchema,
  customerNumber: NonBlankStringSchema,
  customerAlias: NonBlankStringSchema,
  category: z.enum(JEWELRY_CATEGORIES),
  plannedFollowUpAt: IsoTimestampSchema,
  status: z.enum(["pending", "interested", "won", "lost"]),
}).strict();

export const FollowUpRecordSchema = PersistedRecordSchema.extend({
  customerId: NonBlankStringSchema,
  employeeUserId: NonBlankStringSchema,
  idempotencyKey: IdempotencyKeySchema,
  requestFingerprint: RequestFingerprintSchema,
  customerMessageVaultAssetId: NonBlankStringSchema,
  customerMessageSummary: NonBlankStringSchema,
  plannedFollowUpAt: IsoTimestampSchema,
  actualFollowUpAt: IsoTimestampSchema.optional(),
  result: z.enum(["pending", "interested", "won", "lost"]),
  nextAction: NonBlankStringSchema,
  decisionCardId: NonBlankStringSchema,
  cardInstanceId: OptionalNonBlankStringSchema,
  replyUsage: z.enum(["adopted", "edited", "not_used"]),
  chosenReplyId: OptionalNonBlankStringSchema,
  finalEditedText: OptionalNonBlankStringSchema,
}).strict();

export const DecisionUsageRecordSchema = PersistedRecordSchema.extend({
  customerId: NonBlankStringSchema,
  followUpId: NonBlankStringSchema,
  employeeUserId: NonBlankStringSchema,
  decisionCardId: NonBlankStringSchema,
  cardInstanceId: OptionalNonBlankStringSchema,
  cardDigest: z.string().regex(/^sha256_[a-f0-9]{64}$/u).optional(),
  scenario: z.enum(SALES_SCENARIOS),
  deliveryMode: z.literal("copy_only"),
  providedReplyIds: z.array(NonBlankStringSchema).readonly(),
  replyUsage: z.enum(["adopted", "edited", "not_used"]),
  chosenReplyId: OptionalNonBlankStringSchema,
  finalEditedText: OptionalNonBlankStringSchema,
  copiedAt: IsoTimestampSchema.optional(),
  safetyStatus: z.enum(["safe", "reviewRequired"]).optional(),
  safetyRuleIds: z.array(z.enum(SAFETY_RULE_IDS)).readonly().optional(),
}).strict();

export const CorrectionRecordSchema = PersistedRecordSchema.extend({
  customerId: NonBlankStringSchema,
  followUpId: NonBlankStringSchema,
  actorUserId: NonBlankStringSchema,
  idempotencyKey: IdempotencyKeySchema,
  requestFingerprint: RequestFingerprintSchema,
  reason: NonBlankStringSchema,
}).strict();

export const AuditEventSchema = PersistedRecordSchema.extend({
  actorUserId: NonBlankStringSchema.nullable(),
  action: NonBlankStringSchema,
  targetKind: NonBlankStringSchema,
  targetId: NonBlankStringSchema,
  occurredAt: IsoTimestampSchema,
  details: z.record(z.string(), z.unknown()).readonly().optional(),
}).strict();

export const OwnerDashboardRecordSchema = z.object({
  id: NonBlankStringSchema,
  tenantId: NonBlankStringSchema,
  customerId: NonBlankStringSchema,
  customerNumber: NonBlankStringSchema,
  customerAlias: NonBlankStringSchema,
  employeeUserId: NonBlankStringSchema,
  employeeLabel: NonBlankStringSchema,
  category: z.enum(JEWELRY_CATEGORIES),
  submittedAt: IsoTimestampSchema,
  responseStartedAt: IsoTimestampSchema.optional(),
  plannedFollowUpAt: IsoTimestampSchema,
  actualFollowUpAt: IsoTimestampSchema.optional(),
  result: z.enum(["pending", "interested", "won", "lost"]),
  replyUsage: z.enum(["adopted", "edited", "not_used"]),
  nextAction: NonBlankStringSchema,
  decisionCardId: NonBlankStringSchema,
  customerFingerprint: RequestFingerprintSchema,
  safetyReviewRequired: z.boolean().optional(),
  safetyFlags: z.array(z.enum(SAFETY_RULE_IDS)).readonly().optional(),
  retentionFailed: z.boolean().optional(),
  riskLabels: z.array(NonBlankStringSchema).readonly().optional(),
}).strict();

export const RiskAlertSchema = PersistedRecordSchema.extend({
  customerId: OptionalNonBlankStringSchema,
  employeeUserId: OptionalNonBlankStringSchema,
  detectedByUserId: NonBlankStringSchema.nullable(),
  kind: z.enum([
    "overdue_follow_up",
    "missing_result",
    "duplicate_customer",
    "sales_safety",
    "retention_failed",
  ]),
  status: z.enum(["open", "resolved"]),
  resolvedAt: IsoTimestampSchema.optional(),
  resolvedByUserId: OptionalNonBlankStringSchema,
  resolutionNote: OptionalNonBlankStringSchema,
}).strict();

export { VaultAssetRecordSchema, VaultPendingUploadRecordSchema };
