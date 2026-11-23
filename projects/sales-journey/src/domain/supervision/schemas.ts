import { z } from "zod";

import { JEWELRY_CATEGORIES } from "@/domain/decision-card/local-evidence";
import { calculateVaultDeleteAfter } from "@/domain/supervision/contracts";

const NonBlankStringSchema = z.string().trim().min(1);
const IsoTimestampSchema = z.string().datetime();
const OwnerFilterTimestampSchema = z.string()
  .datetime({ offset: true })
  .transform((value) => new Date(value).toISOString());
const FollowUpResultSchema = z.enum(["pending", "interested", "won", "lost"]);
const ReplyUsageSchema = z.enum(["adopted", "edited", "not_used"]);

/**
 * Employee requests deliberately omit tenant and actor identity. The server
 * supplies those values from the authenticated membership.
 */
export const CreateCustomerInputSchema = z.object({
  customerAlias: NonBlankStringSchema.max(40),
  realName: NonBlankStringSchema.max(80).optional(),
  category: z.enum(JEWELRY_CATEGORIES),
  plannedFollowUpAt: IsoTimestampSchema,
}).strict();

export const AppendFollowUpInputSchema = z.object({
  customerMessage: NonBlankStringSchema.max(4_000),
  recentContext: NonBlankStringSchema.max(8_000).optional(),
  plannedFollowUpAt: IsoTimestampSchema,
  actualFollowUpAt: IsoTimestampSchema.optional(),
  result: FollowUpResultSchema,
  nextAction: NonBlankStringSchema.max(1_000),
  decisionCardId: NonBlankStringSchema.max(120),
  cardInstanceId: NonBlankStringSchema.max(120).optional(),
  cardDigest: z.string().regex(/^sha256_[a-f0-9]{64}$/u).optional(),
  replyUsage: ReplyUsageSchema,
  chosenReplyId: NonBlankStringSchema.max(120).optional(),
  finalEditedText: NonBlankStringSchema.max(4_000).optional(),
}).strict();

export const AppendCorrectionInputSchema = z.object({
  followUpId: NonBlankStringSchema.max(120),
  reason: NonBlankStringSchema.max(1_000),
}).strict();

export const OwnerFilterSchema = z.object({
  from: OwnerFilterTimestampSchema.optional(),
  to: OwnerFilterTimestampSchema.optional(),
  employeeUserId: NonBlankStringSchema.max(120).optional(),
  employeeRef: NonBlankStringSchema.max(120).optional(),
  customerNumber: NonBlankStringSchema.max(120).optional(),
  customerQuery: NonBlankStringSchema.max(120).optional(),
  category: z.enum(JEWELRY_CATEGORIES).optional(),
  result: FollowUpResultSchema.optional(),
}).strict().superRefine((filters, context) => {
  if (
    filters.from
    && filters.to
    && new Date(filters.from).getTime() > new Date(filters.to).getTime()
  ) {
    context.addIssue({
      code: "custom",
      message: "开始时间不能晚于结束时间。",
      path: ["from"],
    });
  }
});

/**
 * Repository implementations must parse vault metadata through this schema so
 * a record cannot claim a retention date other than the fixed 30-day expiry.
 */
export const VaultAssetRecordSchema = z.object({
  id: NonBlankStringSchema.max(120),
  tenantId: NonBlankStringSchema.max(120),
  customerId: NonBlankStringSchema.max(120),
  uploadedByUserId: NonBlankStringSchema.max(120),
  kind: z.enum(["customer_identity", "chat_text", "product_image"]),
  vaultKey: NonBlankStringSchema.max(500),
  byteSize: z.number().int().nonnegative().optional(),
  uploadedAt: IsoTimestampSchema,
  deleteAfter: IsoTimestampSchema,
  deletedAt: IsoTimestampSchema.optional(),
  deletedByUserId: NonBlankStringSchema.max(120).optional(),
  version: z.number().int().positive(),
  createdAt: IsoTimestampSchema,
}).strict().superRefine((asset, context) => {
  const uploadedAtMilliseconds = new Date(asset.uploadedAt).getTime();

  if (Number.isNaN(uploadedAtMilliseconds)) {
    return;
  }

  if (asset.deleteAfter !== calculateVaultDeleteAfter(asset.uploadedAt)) {
    context.addIssue({
      code: "custom",
      message: "保险柜资料必须在上传后精确保留 30 天。",
      path: ["deleteAfter"],
    });
  }
});

export const VaultPendingUploadRecordSchema = z.object({
  id: NonBlankStringSchema.max(120),
  tenantId: NonBlankStringSchema.max(120),
  customerId: NonBlankStringSchema.max(120),
  uploadedByUserId: NonBlankStringSchema.max(120),
  vaultKey: NonBlankStringSchema.max(500),
  byteSize: z.number().int().nonnegative(),
  uploadedAt: IsoTimestampSchema,
  deleteAfter: IsoTimestampSchema,
  uploadDay: z.string().regex(/^\d{4}-\d{2}-\d{2}$/u),
  version: z.number().int().positive(),
  createdAt: IsoTimestampSchema,
}).strict().superRefine((upload, context) => {
  if (upload.deleteAfter !== calculateVaultDeleteAfter(upload.uploadedAt)) {
    context.addIssue({
      code: "custom",
      message: "待处理上传必须在上传后精确保留 30 天。",
      path: ["deleteAfter"],
    });
  }
  if (upload.uploadDay !== upload.uploadedAt.slice(0, 10)) {
    context.addIssue({
      code: "custom",
      message: "上传配额日期必须与 UTC 上传日一致。",
      path: ["uploadDay"],
    });
  }
});

export type CreateCustomerInput = z.infer<typeof CreateCustomerInputSchema>;
export type AppendFollowUpInput = z.infer<typeof AppendFollowUpInputSchema>;
export type AppendCorrectionInput = z.infer<typeof AppendCorrectionInputSchema>;
export type OwnerFilter = z.infer<typeof OwnerFilterSchema>;
export type VaultAssetRecordInput = z.infer<typeof VaultAssetRecordSchema>;
export type VaultPendingUploadRecordInput = z.infer<typeof VaultPendingUploadRecordSchema>;
