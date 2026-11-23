import type {
  AuditEvent,
  CorrectionRecord,
  CustomerRecord,
  DecisionUsageRecord,
  FollowUpRecord,
  CreateReplyFeedbackInput,
  OwnerDashboard,
  OwnerReplyQualityQuery,
  ReplyFeedbackRecord,
  ReplyFeedbackSaveResult,
  RiskAlert,
  VaultAssetRecord,
  VaultPendingUploadRecord,
} from "@/domain/supervision/contracts";
import { D1SupervisionRepository, type D1DatabaseLike } from "./d1-repository";
import { MemorySupervisionRepository } from "./memory-repository";

type GeneratedRecordFields = "id" | "version" | "createdAt";
type PersistedRecordShape = Readonly<{ id: string; version: number; createdAt: string }>;
type CreateRecordInput<T extends PersistedRecordShape> = Omit<T, GeneratedRecordFields>
  & Partial<Pick<T, "id" | "createdAt">>;

export type TenantCustomerKey = Readonly<{ tenantId: string; customerId: string }>;
export type TenantEmployeeKey = Readonly<{ tenantId: string; employeeUserId: string }>;
export type TenantVaultKey = Readonly<{ tenantId: string; vaultAssetId: string }>;
export type TenantAuditQuery = Readonly<{
  tenantId: string;
  targetKind: string;
  targetId: string;
}>;
export type EmployeeIdempotencyKey = TenantEmployeeKey & Readonly<{ idempotencyKey: string }>;
export type ActorIdempotencyKey = Readonly<{
  tenantId: string;
  actorUserId: string;
  idempotencyKey: string;
}>;

export type CreateCustomerRecordInput = CreateRecordInput<CustomerRecord>;
export type AppendFollowUpRecordInput = CreateRecordInput<FollowUpRecord>;
export type AppendDecisionUsageRecordInput = CreateRecordInput<DecisionUsageRecord>;
export type AppendCorrectionRecordInput = Readonly<{
  id?: string;
  tenantId: string;
  followUpId: string;
  actorUserId: string;
  idempotencyKey?: string;
  requestFingerprint?: string;
  reason: string;
  createdAt?: string;
}>;
export type CreateVaultAssetRecordInput = Omit<
  CreateRecordInput<VaultAssetRecord>,
  "deletedAt" | "deletedByUserId"
>;
export type CreateVaultPendingUploadRecordInput = CreateRecordInput<VaultPendingUploadRecord>;
export type AppendAuditInput = CreateRecordInput<AuditEvent>;
export type UpsertRiskAlertInput = CreateRecordInput<RiskAlert>;

export type CreateVaultUploadBundleInput = Readonly<{
  assets: readonly CreateVaultAssetRecordInput[];
  audits: readonly AppendAuditInput[];
}>;

export type ReserveVaultUploadBundleInput = Readonly<{
  pendingUploads: readonly CreateVaultPendingUploadRecordInput[];
  dailyByteLimit: number;
}>;

export type VaultExpiryCursor = Readonly<{ deleteAfter: string; id: string }>;
export type TenantPendingVaultKey = Readonly<{
  tenantId: string;
  pendingUploadId: string;
}>;
export type TenantVaultUploadDayKey = Readonly<{ tenantId: string; uploadDay: string }>;

export class VaultDailyQuotaExceededError extends Error {
  constructor() {
    super("vault daily tenant limit exceeded");
    this.name = "VaultDailyQuotaExceededError";
  }
}

export type FinalizeVaultAssetDeletionInput = TenantVaultKey & Readonly<{
  deletedAt: string;
  audit: AppendAuditInput;
}>;

export type CreateCustomerBundleInput = Readonly<{
  customer: CreateCustomerRecordInput;
  vaultAsset?: CreateVaultAssetRecordInput;
  audit: AppendAuditInput;
}>;
export type CreateCustomerBundleResult = Readonly<{
  customer: CustomerRecord;
  vaultAsset?: VaultAssetRecord;
  audit: AuditEvent;
}>;
export type AppendFollowUpBundleInput = Readonly<{
  followUp: AppendFollowUpRecordInput;
  decisionUsage: AppendDecisionUsageRecordInput;
  vaultAsset: CreateVaultAssetRecordInput;
  audit: AppendAuditInput;
}>;
export type AppendFollowUpBundleResult = Readonly<{
  followUp: FollowUpRecord;
  decisionUsage: DecisionUsageRecord;
  vaultAsset: VaultAssetRecord;
  audit: AuditEvent;
}>;
export type AppendCorrectionBundleInput = Readonly<{
  correction: AppendCorrectionRecordInput;
  audit: AppendAuditInput;
}>;
export type AppendCorrectionBundleResult = Readonly<{
  correction: CorrectionRecord;
  audit: AuditEvent;
}>;

export type TimelineEntry =
  | Readonly<{ kind: "follow_up"; record: FollowUpRecord }>
  | Readonly<{ kind: "decision_usage"; record: DecisionUsageRecord }>
  | Readonly<{ kind: "correction"; record: CorrectionRecord }>;

export type OwnerDashboardQuery = Readonly<{
  tenantId: string;
  now: string;
  from?: string;
  to?: string;
  employeeUserId?: string;
  customerNumber?: string;
  customerQuery?: string;
  category?: CustomerRecord["category"];
  result?: FollowUpRecord["result"];
}>;
export type OwnerDashboardSource = OwnerDashboard;

export interface SupervisionRepository {
  createCustomerBundle(input: CreateCustomerBundleInput): Promise<CreateCustomerBundleResult>;
  getCustomerBundleByIdempotencyKey(
    input: EmployeeIdempotencyKey,
  ): Promise<CreateCustomerBundleResult | undefined>;
  appendFollowUpBundle(input: AppendFollowUpBundleInput): Promise<AppendFollowUpBundleResult>;
  getFollowUpBundleByIdempotencyKey(
    input: EmployeeIdempotencyKey,
  ): Promise<AppendFollowUpBundleResult | undefined>;
  appendCorrectionBundle(input: AppendCorrectionBundleInput): Promise<AppendCorrectionBundleResult>;
  getCorrectionBundleByIdempotencyKey(
    input: ActorIdempotencyKey,
  ): Promise<AppendCorrectionBundleResult | undefined>;
  createCustomer(input: CreateCustomerRecordInput): Promise<CustomerRecord>;
  getCustomer(input: TenantCustomerKey): Promise<CustomerRecord | undefined>;
  listEmployeeCustomers(input: TenantEmployeeKey): Promise<CustomerRecord[]>;
  appendFollowUp(input: AppendFollowUpRecordInput): Promise<FollowUpRecord>;
  appendDecisionUsage(input: AppendDecisionUsageRecordInput): Promise<DecisionUsageRecord>;
  appendCorrection(input: AppendCorrectionRecordInput): Promise<CorrectionRecord>;
  listTimeline(input: TenantCustomerKey): Promise<TimelineEntry[]>;
  createVaultAsset(input: CreateVaultAssetRecordInput): Promise<VaultAssetRecord>;
  reserveVaultUploadBundle(
    input: ReserveVaultUploadBundleInput,
  ): Promise<VaultPendingUploadRecord[]>;
  createVaultUploadBundle(input: CreateVaultUploadBundleInput): Promise<VaultAssetRecord[]>;
  getVaultAsset(input: TenantVaultKey): Promise<VaultAssetRecord | undefined>;
  getVaultPendingUpload(input: TenantPendingVaultKey): Promise<VaultPendingUploadRecord | undefined>;
  getVaultDailyUploadUsage(input: TenantVaultUploadDayKey): Promise<number>;
  resolveVaultPendingUpload(input: TenantPendingVaultKey): Promise<void>;
  listVaultTenantIds(): Promise<string[]>;
  listExpiredVaultAssets(input: {
    tenantId: string;
    now: string;
    limit: number;
    after?: VaultExpiryCursor;
  }): Promise<VaultAssetRecord[]>;
  listExpiredVaultPendingUploads(input: {
    tenantId: string;
    now: string;
    limit: number;
    after?: VaultExpiryCursor;
  }): Promise<VaultPendingUploadRecord[]>;
  markVaultAssetDeleted(input: TenantVaultKey & { deletedAt: string }): Promise<void>;
  finalizeVaultAssetDeletion(input: FinalizeVaultAssetDeletionInput): Promise<void>;
  appendAudit(input: AppendAuditInput): Promise<AuditEvent>;
  listAuditEvents(input: TenantAuditQuery): Promise<AuditEvent[]>;
  upsertAlert(input: UpsertRiskAlertInput): Promise<RiskAlert>;
  saveReplyFeedback(input: CreateReplyFeedbackInput): Promise<ReplyFeedbackSaveResult>;
  replaceQuarantinedReplyFeedback(
    input: CreateReplyFeedbackInput,
  ): Promise<ReplyFeedbackSaveResult | undefined>;
  listReplyFeedbackForOwner(input: OwnerReplyQualityQuery): Promise<ReplyFeedbackRecord[]>;
  getOwnerDashboard(input: OwnerDashboardQuery): Promise<OwnerDashboardSource>;
}

export type RepositoryRuntimeOptions = Readonly<{
  d1?: D1DatabaseLike;
}>;

let localRepository: SupervisionRepository | undefined;

/** Uses D1 when a platform binding is present and one process-local store otherwise. */
export function getSupervisionRepository(
  options: RepositoryRuntimeOptions = {},
): SupervisionRepository {
  if (options.d1) {
    return new D1SupervisionRepository(options.d1);
  }

  localRepository ??= new MemorySupervisionRepository();
  return localRepository;
}
