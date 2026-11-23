import type { JewelryCategory } from "@/domain/decision-card/local-evidence";
import type { SafetyRuleId, SalesScenario, SalesStage } from "@/domain/decision-card/types";
import type { PrivacyReasonCode } from "@/domain/reply-feedback/redaction";
import type { ReplyFeedbackReason } from "@/domain/reply-feedback/schema";

export type IsoTimestamp = string;
export type SupervisionRole = "owner" | "employee";
export type FollowUpResult = "pending" | "interested" | "won" | "lost";
export type ReplyUsage = "adopted" | "edited" | "not_used";

const VAULT_RETENTION_MILLISECONDS = 30 * 24 * 60 * 60 * 1_000;

/** Returns the mandatory vault-expiry timestamp: precisely 30 days after upload. */
export function calculateVaultDeleteAfter(uploadedAt: IsoTimestamp): IsoTimestamp {
  const uploadedAtMilliseconds = new Date(uploadedAt).getTime();

  if (Number.isNaN(uploadedAtMilliseconds)) {
    throw new RangeError("uploadedAt must be an ISO timestamp.");
  }

  return new Date(uploadedAtMilliseconds + VAULT_RETENTION_MILLISECONDS).toISOString();
}

type PersistedRecord = Readonly<{
  id: string;
  tenantId: string;
  version: number;
  createdAt: IsoTimestamp;
}>;

export type CustomerRecord = PersistedRecord & Readonly<{
  employeeUserId: string;
  idempotencyKey?: string;
  requestFingerprint?: string;
  customerFingerprint?: string;
  customerNumber: string;
  customerAlias: string;
  category: JewelryCategory;
  plannedFollowUpAt: IsoTimestamp;
  status: FollowUpResult;
}>;

export type FollowUpRecord = PersistedRecord & Readonly<{
  customerId: string;
  employeeUserId: string;
  idempotencyKey?: string;
  requestFingerprint?: string;
  customerMessageVaultAssetId: string;
  customerMessageSummary: string;
  plannedFollowUpAt: IsoTimestamp;
  actualFollowUpAt?: IsoTimestamp;
  result: FollowUpResult;
  nextAction: string;
  decisionCardId: string;
  cardInstanceId?: string;
  replyUsage: ReplyUsage;
  chosenReplyId?: string;
  finalEditedText?: string;
}>;

export type DecisionUsageRecord = PersistedRecord & Readonly<{
  customerId: string;
  followUpId: string;
  employeeUserId: string;
  decisionCardId: string;
  cardInstanceId?: string;
  cardDigest?: string;
  scenario: SalesScenario;
  deliveryMode: "copy_only";
  providedReplyIds: readonly string[];
  replyUsage: ReplyUsage;
  chosenReplyId?: string;
  finalEditedText?: string;
  copiedAt?: IsoTimestamp;
  safetyStatus?: "safe" | "reviewRequired";
  safetyRuleIds?: readonly SafetyRuleId[];
}>;

export type CorrectionRecord = PersistedRecord & Readonly<{
  customerId: string;
  followUpId: string;
  actorUserId: string;
  idempotencyKey?: string;
  requestFingerprint?: string;
  reason: string;
}>;

export type VaultAssetKind = "customer_identity" | "chat_text" | "product_image";

export type VaultAssetRecord = PersistedRecord & Readonly<{
  customerId: string;
  uploadedByUserId: string;
  kind: VaultAssetKind;
  vaultKey: string;
  byteSize?: number;
  uploadedAt: IsoTimestamp;
  deleteAfter: IsoTimestamp;
  deletedAt?: IsoTimestamp;
  deletedByUserId?: string;
}>;

/** Durable pre-upload ledger entry used to govern ambiguous object-store writes. */
export type VaultPendingUploadRecord = PersistedRecord & Readonly<{
  customerId: string;
  uploadedByUserId: string;
  vaultKey: string;
  byteSize: number;
  uploadedAt: IsoTimestamp;
  deleteAfter: IsoTimestamp;
  uploadDay: string;
}>;

export type AuditEvent = PersistedRecord & Readonly<{
  actorUserId: string | null;
  action: string;
  targetKind: string;
  targetId: string;
  occurredAt: IsoTimestamp;
  details?: Readonly<Record<string, unknown>>;
}>;

export type RiskAlertKind =
  | "overdue_follow_up"
  | "missing_result"
  | "duplicate_customer"
  | "sales_safety"
  | "retention_failed";
export type RiskAlertStatus = "open" | "resolved";

export type RiskAlert = PersistedRecord & Readonly<{
  customerId?: string;
  employeeUserId?: string;
  detectedByUserId: string | null;
  kind: RiskAlertKind;
  status: RiskAlertStatus;
  resolvedAt?: IsoTimestamp;
  resolvedByUserId?: string;
  resolutionNote?: string;
}>;

export type OwnerDashboardMetricKey =
  | "submitted"
  | "followedUp"
  | "pending"
  | "due"
  | "overdue"
  | "interested"
  | "won"
  | "lost"
  | "adopted"
  | "edited"
  | "notUsed"
  | "missingResult"
  | "averageResponseDurationMs"
  | "completionRate";

export type OwnerDashboardDrillDown = Readonly<
  Record<OwnerDashboardMetricKey, readonly string[]>
>;

export type OwnerDashboardMetrics = Readonly<{
  submitted: number;
  followedUp: number;
  pending: number;
  due: number;
  overdue: number;
  interested: number;
  won: number;
  lost: number;
  adopted: number;
  edited: number;
  notUsed: number;
  missingResult: number;
  averageResponseDurationMs: number | null;
  completionRate: number;
  drillDownRecordIds: OwnerDashboardDrillDown;
}>;

export type OwnerDashboardEmployee = OwnerDashboardMetrics & Readonly<{
  employeeUserId: string;
  employeeLabel: string;
}>;

/** Internal, de-identified source row used by pure metrics and risk rules. */
export type OwnerDashboardRecord = Readonly<{
  id: string;
  tenantId: string;
  customerId: string;
  customerNumber: string;
  customerAlias: string;
  employeeUserId: string;
  employeeLabel: string;
  category: JewelryCategory;
  submittedAt: IsoTimestamp;
  responseStartedAt?: IsoTimestamp;
  plannedFollowUpAt: IsoTimestamp;
  actualFollowUpAt?: IsoTimestamp;
  result: FollowUpResult;
  replyUsage: ReplyUsage;
  nextAction: string;
  decisionCardId: string;
  customerFingerprint?: string;
  safetyReviewRequired?: boolean;
  safetyFlags?: readonly string[];
  retentionFailed?: boolean;
  riskLabels?: readonly string[];
}>;

/** Tenant-wide customer identity source used only for duplicate-review signals. */
export type OwnerCustomerRiskRecord = Readonly<{
  id: string;
  tenantId: string;
  customerId: string;
  employeeUserId: string;
  customerFingerprint?: string;
}>;

/** Safe owner-facing row. Internal fingerprints and vault references are absent. */
export type OwnerDashboardRow = Readonly<{
  id: string;
  customerNumber: string;
  customerAlias: string;
  employeeUserId: string;
  employeeLabel: string;
  category: JewelryCategory;
  submittedAt: IsoTimestamp;
  plannedFollowUpAt: IsoTimestamp;
  actualFollowUpAt?: IsoTimestamp;
  decisionUsage: ReplyUsage;
  nextAction: string;
  result: FollowUpResult;
  riskLabels: readonly string[];
}>;

/** Review-only operational signal. It intentionally has no score or grade. */
export type OwnerRiskSignal = Readonly<{
  id: string;
  tenantId: string;
  kind: RiskAlertKind;
  customerId?: string;
  employeeUserId?: string;
  recordIds: readonly string[];
  labels: readonly string[];
  detectedAt: IsoTimestamp;
  sourceAlertId?: string;
}>;

export type OwnerDashboard = Readonly<{
  tenantId: string;
  generatedAt: IsoTimestamp;
  totals: OwnerDashboardMetrics;
  employees: readonly OwnerDashboardEmployee[];
  records: readonly OwnerDashboardRow[];
  riskSignals: readonly OwnerRiskSignal[];
  alerts: readonly RiskAlert[];
}>;

type ReplyFeedbackBase = Readonly<{
  id: string;
  tenantId: string;
  employeeUserId: string;
  customerId: string;
  decisionCardId: string;
  cardInstanceId: string;
  cardDigest: string;
  agentVersion: string;
  rating: "helpful" | "needs_improvement";
  reasonCodes: readonly ReplyFeedbackReason[];
  scenario: SalesScenario;
  salesStage: SalesStage;
  evidenceIds: readonly string[];
  privacyReasonCodes: readonly PrivacyReasonCode[];
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}>;

/** A safe row may retain only the sanitizer's final, privacy-safe text. */
export type ReplyFeedbackRecord =
  | (ReplyFeedbackBase & Readonly<{
    privacyStatus: "safe";
    sanitizedFinalReply?: string;
  }>)
  | (ReplyFeedbackBase & Readonly<{
    privacyStatus: "quarantined";
    sanitizedFinalReply?: never;
  }>);

type CreateReplyFeedbackBase = Omit<
  ReplyFeedbackBase,
  "id" | "createdAt" | "updatedAt"
>;

/** Server-derived repository input. Browser values must be verified first. */
export type CreateReplyFeedbackInput =
  | (CreateReplyFeedbackBase & Readonly<{
    privacyStatus: "safe";
    sanitizedFinalReply?: string;
  }>)
  | (CreateReplyFeedbackBase & Readonly<{
    privacyStatus: "quarantined";
    sanitizedFinalReply?: never;
  }>);

export type ReplyFeedbackSaveResult = Readonly<{
  outcome: "created" | "updated" | "unchanged";
  record: ReplyFeedbackRecord;
}>;

/** Tenant scope is mandatory; all owner-facing filters are optional. */
export type OwnerReplyQualityQuery = Readonly<{
  tenantId: string;
  employeeUserId?: string;
  scenario?: SalesScenario;
  salesStage?: SalesStage;
  agentVersion?: string;
}>;

export type OwnerReplyQualityView = Readonly<{
  totals: Readonly<{
    helpful: number;
    needsImprovement: number;
    quarantined: number;
  }>;
  reasons: readonly Readonly<{
    code: ReplyFeedbackReason;
    count: number;
  }>[];
  samples: readonly (
    | Readonly<{
      feedbackRef: string;
      employeeRef: string;
      customerRef: string;
      scenario: SalesScenario;
      salesStage: SalesStage;
      agentVersion: string;
      rating: "helpful" | "needs_improvement";
      privacyStatus: "safe";
      sanitizedFinalReply?: string;
      privacyReasonCodes: readonly PrivacyReasonCode[];
    }>
    | Readonly<{
      feedbackRef: string;
      employeeRef: string;
      customerRef: string;
      scenario: SalesScenario;
      salesStage: SalesStage;
      agentVersion: string;
      rating: "helpful" | "needs_improvement";
      privacyStatus: "quarantined";
      privacyReasonCodes: readonly PrivacyReasonCode[];
      sanitizedFinalReply?: never;
    }>
  )[];
}>;
