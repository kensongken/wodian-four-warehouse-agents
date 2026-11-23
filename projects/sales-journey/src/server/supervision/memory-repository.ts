import type {
  AuditEvent,
  CorrectionRecord,
  CustomerRecord,
  DecisionUsageRecord,
  FollowUpRecord,
  OwnerDashboardRecord,
  CreateReplyFeedbackInput,
  OwnerReplyQualityQuery,
  ReplyFeedbackRecord,
  ReplyFeedbackSaveResult,
  RiskAlert,
  VaultAssetRecord,
  VaultPendingUploadRecord,
} from "@/domain/supervision/contracts";
import { aggregateOwnerDashboard } from "@/domain/supervision/dashboard";
import {
  evaluateRiskAlerts,
  riskLabelsForDashboardRecord,
} from "@/domain/supervision/risk";
import { OwnerFilterSchema } from "@/domain/supervision/schemas";
import { ownerCustomerMatches } from "@/domain/supervision/customer-query";
import {
  CreateReplyFeedbackInputSchema,
  ReplyFeedbackRecordSchema,
} from "@/domain/reply-feedback/schema";
import type {
  AppendAuditInput,
  AppendCorrectionRecordInput,
  AppendCorrectionBundleInput,
  AppendCorrectionBundleResult,
  AppendDecisionUsageRecordInput,
  AppendFollowUpRecordInput,
  AppendFollowUpBundleInput,
  AppendFollowUpBundleResult,
  ActorIdempotencyKey,
  CreateVaultUploadBundleInput,
  ReserveVaultUploadBundleInput,
  CreateCustomerRecordInput,
  CreateCustomerBundleInput,
  CreateCustomerBundleResult,
  CreateVaultAssetRecordInput,
  EmployeeIdempotencyKey,
  FinalizeVaultAssetDeletionInput,
  OwnerDashboardQuery,
  OwnerDashboardSource,
  SupervisionRepository,
  TenantCustomerKey,
  TenantEmployeeKey,
  TenantAuditQuery,
  TenantVaultKey,
  TenantPendingVaultKey,
  TenantVaultUploadDayKey,
  TimelineEntry,
  UpsertRiskAlertInput,
  VaultExpiryCursor,
} from "./repository";
import { VaultDailyQuotaExceededError } from "./repository";
import {
  AuditEventSchema,
  CorrectionRecordSchema,
  CustomerRecordSchema,
  DecisionUsageRecordSchema,
  FollowUpRecordSchema,
  RiskAlertSchema,
  VaultAssetRecordSchema,
  VaultPendingUploadRecordSchema,
} from "./record-schemas";
import { normalizeExpiredVaultAssetLimit } from "./retention-limit";

type MemorySeed = Readonly<{
  customers?: readonly CustomerRecord[];
  followUps?: readonly FollowUpRecord[];
  decisionUsages?: readonly DecisionUsageRecord[];
  corrections?: readonly CorrectionRecord[];
  vaultAssets?: readonly VaultAssetRecord[];
  audits?: readonly AuditEvent[];
  alerts?: readonly RiskAlert[];
  replyFeedback?: readonly ReplyFeedbackRecord[];
}>;

type MemoryRepositoryOptions = Readonly<{
  seed?: MemorySeed;
  now?: () => string;
  createId?: () => string;
}>;

function defaultNow(): string {
  return new Date().toISOString();
}

function defaultCreateId(): string {
  return crypto.randomUUID();
}

export class MemorySupervisionRepository implements SupervisionRepository {
  private readonly customers: CustomerRecord[];
  private readonly followUps: FollowUpRecord[];
  private readonly decisionUsages: DecisionUsageRecord[];
  private readonly corrections: CorrectionRecord[];
  private readonly vaultAssets: VaultAssetRecord[];
  private readonly vaultPendingUploads: VaultPendingUploadRecord[];
  private readonly vaultDailyQuotas = new Map<string, { usedBytes: number; byteLimit: number }>();
  private readonly audits: AuditEvent[];
  private readonly alerts: RiskAlert[];
  private readonly replyFeedback: ReplyFeedbackRecord[];
  private readonly now: () => string;
  private readonly createId: () => string;

  constructor(options: MemoryRepositoryOptions = {}) {
    this.now = options.now ?? defaultNow;
    this.createId = options.createId ?? defaultCreateId;
    this.customers = (options.seed?.customers ?? []).map((record) => CustomerRecordSchema.parse(record));
    this.followUps = (options.seed?.followUps ?? []).map((record) => FollowUpRecordSchema.parse(record));
    this.decisionUsages = (options.seed?.decisionUsages ?? []).map((record) => DecisionUsageRecordSchema.parse(record));
    this.corrections = (options.seed?.corrections ?? []).map((record) => CorrectionRecordSchema.parse(record));
    this.vaultAssets = (options.seed?.vaultAssets ?? []).map((record) => VaultAssetRecordSchema.parse(record));
    this.vaultPendingUploads = [];
    this.audits = (options.seed?.audits ?? []).map((record) => AuditEventSchema.parse(record));
    this.alerts = (options.seed?.alerts ?? []).map((record) => RiskAlertSchema.parse(record));
    this.replyFeedback = (options.seed?.replyFeedback ?? []).map(
      (record) => ReplyFeedbackRecordSchema.parse(record),
    );
  }

  async createCustomerBundle(
    input: CreateCustomerBundleInput,
  ): Promise<CreateCustomerBundleResult> {
    const customer = this.buildCustomer(input.customer);
    const audit = this.buildAudit(input.audit);
    const vaultAsset = input.vaultAsset ? this.buildVaultAsset(input.vaultAsset) : undefined;

    this.assertUnique(this.customers, customer.tenantId, customer.id, "customer");
    this.assertUniqueIdempotency(
      this.customers,
      customer.tenantId,
      customer.employeeUserId,
      customer.idempotencyKey,
      "customer",
    );
    this.assertUnique(this.audits, audit.tenantId, audit.id, "audit");
    if (vaultAsset) {
      this.assertUnique(this.vaultAssets, vaultAsset.tenantId, vaultAsset.id, "vault asset");
      this.assertSameCustomer(customer, vaultAsset.tenantId, vaultAsset.customerId);
      if (vaultAsset.uploadedByUserId !== customer.employeeUserId) {
        throw new Error("vault uploader mismatch");
      }
    }
    this.assertAuditTarget(audit, customer.tenantId, "customer", customer.id);
    this.assertAuditActor(audit, customer.employeeUserId);

    this.customers.push(customer);
    if (vaultAsset) this.vaultAssets.push(vaultAsset);
    this.audits.push(audit);
    return { customer, vaultAsset, audit };
  }

  async getCustomerBundleByIdempotencyKey(
    input: EmployeeIdempotencyKey,
  ): Promise<CreateCustomerBundleResult | undefined> {
    const customer = this.customers.find((record) => (
      record.tenantId === input.tenantId
      && record.employeeUserId === input.employeeUserId
      && record.idempotencyKey === input.idempotencyKey
    ));
    if (!customer) return undefined;
    const audit = this.audits.find((record) => (
      record.tenantId === input.tenantId
      && record.targetKind === "customer"
      && record.targetId === customer.id
    ));
    if (!audit) throw new Error("customer audit not found");
    const vaultAsset = this.vaultAssets.find((record) => (
      record.tenantId === input.tenantId
      && record.customerId === customer.id
      && record.kind === "customer_identity"
    ));
    return {
      customer: CustomerRecordSchema.parse(customer),
      vaultAsset: vaultAsset ? VaultAssetRecordSchema.parse(vaultAsset) : undefined,
      audit: AuditEventSchema.parse(audit),
    };
  }

  async appendFollowUpBundle(
    input: AppendFollowUpBundleInput,
  ): Promise<AppendFollowUpBundleResult> {
    const customer = this.requireCustomer(input.followUp.tenantId, input.followUp.customerId);
    const followUp = this.buildFollowUp(input.followUp);
    const decisionUsage = this.buildDecisionUsage(input.decisionUsage);
    const vaultAsset = this.buildVaultAsset(input.vaultAsset);
    const audit = this.buildAudit(input.audit);

    this.assertUnique(this.followUps, followUp.tenantId, followUp.id, "follow-up");
    this.assertUniqueIdempotency(
      this.followUps,
      followUp.tenantId,
      followUp.employeeUserId,
      followUp.idempotencyKey,
      "follow-up",
    );
    this.assertUnique(this.decisionUsages, decisionUsage.tenantId, decisionUsage.id, "decision usage");
    this.assertUnique(this.vaultAssets, vaultAsset.tenantId, vaultAsset.id, "vault asset");
    this.assertUnique(this.audits, audit.tenantId, audit.id, "audit");
    this.assertSameCustomer(customer, followUp.tenantId, followUp.customerId);
    this.assertSameCustomer(customer, decisionUsage.tenantId, decisionUsage.customerId);
    this.assertSameCustomer(customer, vaultAsset.tenantId, vaultAsset.customerId);
    this.assertDecisionUsageMatches(followUp, decisionUsage);
    if (vaultAsset.uploadedByUserId !== followUp.employeeUserId) {
      throw new Error("vault uploader mismatch");
    }
    if (followUp.customerMessageVaultAssetId !== vaultAsset.id) {
      throw new Error("follow-up vault reference mismatch");
    }
    this.assertAuditTarget(audit, followUp.tenantId, "follow_up", followUp.id);
    this.assertAuditActor(audit, followUp.employeeUserId);

    this.followUps.push(followUp);
    this.decisionUsages.push(decisionUsage);
    this.vaultAssets.push(vaultAsset);
    this.audits.push(audit);
    return { followUp, decisionUsage, vaultAsset, audit };
  }

  async getFollowUpBundleByIdempotencyKey(
    input: EmployeeIdempotencyKey,
  ): Promise<AppendFollowUpBundleResult | undefined> {
    const followUp = this.followUps.find((record) => (
      record.tenantId === input.tenantId
      && record.employeeUserId === input.employeeUserId
      && record.idempotencyKey === input.idempotencyKey
    ));
    if (!followUp) return undefined;
    const decisionUsage = this.decisionUsages.find((record) => (
      record.tenantId === input.tenantId && record.followUpId === followUp.id
    ));
    const vaultAsset = this.vaultAssets.find((record) => (
      record.tenantId === input.tenantId && record.id === followUp.customerMessageVaultAssetId
    ));
    const audit = this.audits.find((record) => (
      record.tenantId === input.tenantId
      && record.targetKind === "follow_up"
      && record.targetId === followUp.id
    ));
    if (!decisionUsage || !vaultAsset || !audit) throw new Error("follow-up receipt incomplete");
    return {
      followUp: FollowUpRecordSchema.parse(followUp),
      decisionUsage: DecisionUsageRecordSchema.parse(decisionUsage),
      vaultAsset: VaultAssetRecordSchema.parse(vaultAsset),
      audit: AuditEventSchema.parse(audit),
    };
  }

  async appendCorrectionBundle(
    input: AppendCorrectionBundleInput,
  ): Promise<AppendCorrectionBundleResult> {
    const followUp = this.requireFollowUp(input.correction.tenantId, input.correction.followUpId);
    const correction = this.buildCorrection(input.correction, followUp);
    const audit = this.buildAudit(input.audit);

    this.assertUnique(this.corrections, correction.tenantId, correction.id, "correction");
    this.assertUniqueIdempotency(
      this.corrections,
      correction.tenantId,
      correction.actorUserId,
      correction.idempotencyKey,
      "correction",
    );
    this.assertUnique(this.audits, audit.tenantId, audit.id, "audit");
    this.assertAuditTarget(audit, correction.tenantId, "correction", correction.id);
    this.assertAuditActor(audit, correction.actorUserId);

    this.corrections.push(correction);
    this.audits.push(audit);
    return { correction, audit };
  }

  async getCorrectionBundleByIdempotencyKey(
    input: ActorIdempotencyKey,
  ): Promise<AppendCorrectionBundleResult | undefined> {
    const correction = this.corrections.find((record) => (
      record.tenantId === input.tenantId
      && record.actorUserId === input.actorUserId
      && record.idempotencyKey === input.idempotencyKey
    ));
    if (!correction) return undefined;
    const audit = this.audits.find((record) => (
      record.tenantId === input.tenantId
      && record.targetKind === "correction"
      && record.targetId === correction.id
    ));
    if (!audit) throw new Error("correction audit not found");
    return {
      correction: CorrectionRecordSchema.parse(correction),
      audit: AuditEventSchema.parse(audit),
    };
  }

  async createCustomer(input: CreateCustomerRecordInput): Promise<CustomerRecord> {
    const record = this.buildCustomer(input);
    this.assertUnique(this.customers, record.tenantId, record.id, "customer");
    this.customers.push(record);
    return CustomerRecordSchema.parse(record);
  }

  async getCustomer(input: TenantCustomerKey): Promise<CustomerRecord | undefined> {
    const record = this.customers.find(
      (candidate) => candidate.tenantId === input.tenantId && candidate.id === input.customerId,
    );
    return record ? CustomerRecordSchema.parse(record) : undefined;
  }

  async listEmployeeCustomers(input: TenantEmployeeKey): Promise<CustomerRecord[]> {
    return this.customers
      .filter((record) => (
        record.tenantId === input.tenantId && record.employeeUserId === input.employeeUserId
      ))
      .map((record) => CustomerRecordSchema.parse(record));
  }

  async appendFollowUp(input: AppendFollowUpRecordInput): Promise<FollowUpRecord> {
    this.requireCustomer(input.tenantId, input.customerId);
    const record = this.buildFollowUp(input);
    this.assertUnique(this.followUps, record.tenantId, record.id, "follow-up");
    this.followUps.push(record);
    return FollowUpRecordSchema.parse(record);
  }

  async appendDecisionUsage(
    input: AppendDecisionUsageRecordInput,
  ): Promise<DecisionUsageRecord> {
    this.requireCustomer(input.tenantId, input.customerId);
    const followUp = this.requireFollowUp(input.tenantId, input.followUpId);
    if (followUp.customerId !== input.customerId) {
      throw new Error("follow-up customer mismatch");
    }
    const record = this.buildDecisionUsage(input);
    this.assertUnique(this.decisionUsages, record.tenantId, record.id, "decision usage");
    this.decisionUsages.push(record);
    return DecisionUsageRecordSchema.parse(record);
  }

  async appendCorrection(input: AppendCorrectionRecordInput): Promise<CorrectionRecord> {
    const followUp = this.requireFollowUp(input.tenantId, input.followUpId);
    const record = this.buildCorrection(input, followUp);
    this.assertUnique(this.corrections, record.tenantId, record.id, "correction");
    this.corrections.push(record);
    return CorrectionRecordSchema.parse(record);
  }

  async listTimeline(input: TenantCustomerKey): Promise<TimelineEntry[]> {
    const entries: TimelineEntry[] = [
      ...this.followUps
        .filter((record) => record.tenantId === input.tenantId && record.customerId === input.customerId)
        .map((record): TimelineEntry => ({ kind: "follow_up", record: FollowUpRecordSchema.parse(record) })),
      ...this.decisionUsages
        .filter((record) => record.tenantId === input.tenantId && record.customerId === input.customerId)
        .map((record): TimelineEntry => ({
          kind: "decision_usage",
          record: DecisionUsageRecordSchema.parse(record),
        })),
      ...this.corrections
        .filter((record) => record.tenantId === input.tenantId && record.customerId === input.customerId)
        .map((record): TimelineEntry => ({ kind: "correction", record: CorrectionRecordSchema.parse(record) })),
    ];

    return entries.sort((left, right) => left.record.createdAt.localeCompare(right.record.createdAt));
  }

  async createVaultAsset(input: CreateVaultAssetRecordInput): Promise<VaultAssetRecord> {
    this.requireCustomer(input.tenantId, input.customerId);
    const record = this.buildVaultAsset(input);
    this.assertUnique(this.vaultAssets, record.tenantId, record.id, "vault asset");
    this.vaultAssets.push(record);
    return VaultAssetRecordSchema.parse(record);
  }

  async reserveVaultUploadBundle(
    input: ReserveVaultUploadBundleInput,
  ): Promise<VaultPendingUploadRecord[]> {
    if (input.pendingUploads.length === 0) throw new Error("vault upload reservation is empty");
    if (!Number.isSafeInteger(input.dailyByteLimit) || input.dailyByteLimit <= 0) {
      throw new RangeError("dailyByteLimit must be a positive integer");
    }
    const pendingUploads = input.pendingUploads.map((upload) => this.buildVaultPendingUpload(upload));
    const tenantId = pendingUploads[0]!.tenantId;
    const uploadDay = pendingUploads[0]!.uploadDay;
    const candidateIds = new Set<string>();
    const candidateKeys = new Set<string>();
    for (const upload of pendingUploads) {
      const customer = this.requireCustomer(upload.tenantId, upload.customerId);
      if (
        upload.tenantId !== tenantId
        || upload.uploadDay !== uploadDay
        || customer.employeeUserId !== upload.uploadedByUserId
      ) {
        throw new Error("vault upload reservation actor mismatch");
      }
      if (candidateIds.has(upload.id) || candidateKeys.has(upload.vaultKey)) {
        throw new Error("vault upload reservation candidate already exists");
      }
      candidateIds.add(upload.id);
      candidateKeys.add(upload.vaultKey);
      this.assertUnique(this.vaultPendingUploads, upload.tenantId, upload.id, "pending vault upload");
      if (this.vaultPendingUploads.some((record) => (
        record.tenantId === upload.tenantId && record.vaultKey === upload.vaultKey
      ))) {
        throw new Error("pending vault key already exists");
      }
    }

    const quotaKey = `${tenantId}\u0000${uploadDay}`;
    const quota = this.vaultDailyQuotas.get(quotaKey) ?? {
      usedBytes: 0,
      byteLimit: input.dailyByteLimit,
    };
    const candidateBytes = pendingUploads.reduce((total, upload) => total + upload.byteSize, 0);
    if (quota.usedBytes + candidateBytes > input.dailyByteLimit) {
      throw new VaultDailyQuotaExceededError();
    }

    this.vaultDailyQuotas.set(quotaKey, {
      usedBytes: quota.usedBytes + candidateBytes,
      byteLimit: input.dailyByteLimit,
    });
    this.vaultPendingUploads.push(...pendingUploads);
    return pendingUploads.map((upload) => VaultPendingUploadRecordSchema.parse(upload));
  }

  async createVaultUploadBundle(
    input: CreateVaultUploadBundleInput,
  ): Promise<VaultAssetRecord[]> {
    if (input.assets.length === 0 || input.assets.length !== input.audits.length) {
      throw new Error("vault upload bundle mismatch");
    }

    const assets = input.assets.map((asset) => this.buildVaultAsset(asset));
    const audits = input.audits.map((audit) => this.buildAudit(audit));
    const tenantId = assets[0]!.tenantId;
    const candidateIds = new Set<string>();
    const candidateKeys = new Set<string>();
    const candidateAuditIds = new Set<string>();

    for (let index = 0; index < assets.length; index += 1) {
      const asset = assets[index]!;
      const audit = audits[index]!;
      const customer = this.requireCustomer(asset.tenantId, asset.customerId);
      if (asset.tenantId !== tenantId || customer.employeeUserId !== asset.uploadedByUserId) {
        throw new Error("vault upload actor mismatch");
      }
      if (candidateIds.has(asset.id) || candidateKeys.has(asset.vaultKey)) {
        throw new Error("vault upload candidate already exists");
      }
      if (candidateAuditIds.has(audit.id)) throw new Error("audit candidate already exists");
      candidateIds.add(asset.id);
      candidateKeys.add(asset.vaultKey);
      candidateAuditIds.add(audit.id);
      this.assertUnique(this.vaultAssets, asset.tenantId, asset.id, "vault asset");
      if (this.vaultAssets.some((record) => (
        record.tenantId === asset.tenantId && record.vaultKey === asset.vaultKey
      ))) {
        throw new Error("vault key already exists");
      }
      this.assertUnique(this.audits, audit.tenantId, audit.id, "audit");
      this.assertAuditTarget(audit, asset.tenantId, "vault_asset", asset.id);
      this.assertAuditActor(audit, asset.uploadedByUserId);
      const pending = this.vaultPendingUploads.find((record) => (
        record.tenantId === asset.tenantId && record.id === asset.id
      ));
      if (
        !pending
        || pending.customerId !== asset.customerId
        || pending.uploadedByUserId !== asset.uploadedByUserId
        || pending.vaultKey !== asset.vaultKey
        || pending.byteSize !== (asset.byteSize ?? 0)
        || pending.uploadedAt !== asset.uploadedAt
        || pending.deleteAfter !== asset.deleteAfter
      ) {
        throw new Error("vault upload reservation mismatch");
      }
    }

    this.vaultAssets.push(...assets);
    this.audits.push(...audits);
    const committedIds = new Set(assets.map((asset) => `${asset.tenantId}\u0000${asset.id}`));
    for (let index = this.vaultPendingUploads.length - 1; index >= 0; index -= 1) {
      const pending = this.vaultPendingUploads[index]!;
      if (committedIds.has(`${pending.tenantId}\u0000${pending.id}`)) {
        this.vaultPendingUploads.splice(index, 1);
      }
    }
    return assets.map((record) => VaultAssetRecordSchema.parse(record));
  }

  async getVaultAsset(input: TenantVaultKey): Promise<VaultAssetRecord | undefined> {
    const record = this.vaultAssets.find((candidate) => (
      candidate.tenantId === input.tenantId && candidate.id === input.vaultAssetId
    ));
    return record ? VaultAssetRecordSchema.parse(record) : undefined;
  }

  async getVaultPendingUpload(
    input: TenantPendingVaultKey,
  ): Promise<VaultPendingUploadRecord | undefined> {
    const record = this.vaultPendingUploads.find((candidate) => (
      candidate.tenantId === input.tenantId && candidate.id === input.pendingUploadId
    ));
    return record ? VaultPendingUploadRecordSchema.parse(record) : undefined;
  }

  async getVaultDailyUploadUsage(input: TenantVaultUploadDayKey): Promise<number> {
    return this.vaultDailyQuotas.get(`${input.tenantId}\u0000${input.uploadDay}`)?.usedBytes ?? 0;
  }

  async resolveVaultPendingUpload(input: TenantPendingVaultKey): Promise<void> {
    const index = this.vaultPendingUploads.findIndex((candidate) => (
      candidate.tenantId === input.tenantId && candidate.id === input.pendingUploadId
    ));
    if (index < 0) return;
    const [pending] = this.vaultPendingUploads.splice(index, 1);
    const quotaKey = `${pending!.tenantId}\u0000${pending!.uploadDay}`;
    const quota = this.vaultDailyQuotas.get(quotaKey);
    if (quota) {
      this.vaultDailyQuotas.set(quotaKey, {
        ...quota,
        usedBytes: Math.max(0, quota.usedBytes - pending!.byteSize),
      });
    }
  }

  async listVaultTenantIds(): Promise<string[]> {
    return [...new Set([
      ...this.vaultAssets.map((record) => record.tenantId),
      ...this.vaultPendingUploads.map((record) => record.tenantId),
    ])].sort();
  }

  async listExpiredVaultAssets(input: {
    tenantId: string;
    now: string;
    limit: number;
    after?: VaultExpiryCursor;
  }): Promise<VaultAssetRecord[]> {
    return this.vaultAssets
      .filter((record) => (
        record.tenantId === input.tenantId
        && record.deletedAt === undefined
        && record.deleteAfter <= input.now
        && (!input.after || (
          record.deleteAfter > input.after.deleteAfter
          || (record.deleteAfter === input.after.deleteAfter && record.id > input.after.id)
        ))
      ))
      .sort((left, right) => (
        left.deleteAfter.localeCompare(right.deleteAfter) || left.id.localeCompare(right.id)
      ))
      .slice(0, normalizeExpiredVaultAssetLimit(input.limit))
      .map((record) => VaultAssetRecordSchema.parse(record));
  }

  async listExpiredVaultPendingUploads(input: {
    tenantId: string;
    now: string;
    limit: number;
    after?: VaultExpiryCursor;
  }): Promise<VaultPendingUploadRecord[]> {
    return this.vaultPendingUploads
      .filter((record) => (
        record.tenantId === input.tenantId
        && record.deleteAfter <= input.now
        && (!input.after || (
          record.deleteAfter > input.after.deleteAfter
          || (record.deleteAfter === input.after.deleteAfter && record.id > input.after.id)
        ))
      ))
      .sort((left, right) => (
        left.deleteAfter.localeCompare(right.deleteAfter) || left.id.localeCompare(right.id)
      ))
      .slice(0, normalizeExpiredVaultAssetLimit(input.limit))
      .map((record) => VaultPendingUploadRecordSchema.parse(record));
  }

  async markVaultAssetDeleted(
    input: TenantVaultKey & { deletedAt: string },
  ): Promise<void> {
    const index = this.vaultAssets.findIndex(
      (record) => record.tenantId === input.tenantId && record.id === input.vaultAssetId,
    );
    if (index < 0) {
      throw new Error("vault asset not found");
    }
    this.vaultAssets[index] = VaultAssetRecordSchema.parse({
      ...this.vaultAssets[index],
      deletedAt: input.deletedAt,
      version: this.vaultAssets[index]!.version + 1,
    });
  }

  async finalizeVaultAssetDeletion(input: FinalizeVaultAssetDeletionInput): Promise<void> {
    const index = this.vaultAssets.findIndex((record) => (
      record.tenantId === input.tenantId && record.id === input.vaultAssetId
    ));
    if (index < 0) throw new Error("vault asset not found");
    if (this.vaultAssets[index]!.deletedAt !== undefined) return;

    const audit = this.buildAudit({
      ...input.audit,
      id: `vault-deleted-${input.vaultAssetId}`,
    });
    this.assertUnique(this.audits, audit.tenantId, audit.id, "audit");
    this.assertAuditTarget(audit, input.tenantId, "vault_asset", input.vaultAssetId);
    if (audit.actorUserId !== null || audit.action !== "vault.deleted") {
      throw new Error("invalid retention audit");
    }
    const updated = VaultAssetRecordSchema.parse({
      ...this.vaultAssets[index],
      vaultKey: `deleted/${input.vaultAssetId}`,
      deletedAt: input.deletedAt,
      version: this.vaultAssets[index]!.version + 1,
    });
    this.vaultAssets[index] = updated;
    this.audits.push(audit);
  }

  async appendAudit(input: AppendAuditInput): Promise<AuditEvent> {
    const record = this.buildAudit(input);
    this.assertUnique(this.audits, record.tenantId, record.id, "audit");
    this.audits.push(record);
    return AuditEventSchema.parse(record);
  }

  async listAuditEvents(input: TenantAuditQuery): Promise<AuditEvent[]> {
    return this.audits
      .filter((record) => (
        record.tenantId === input.tenantId
        && record.targetKind === input.targetKind
        && record.targetId === input.targetId
      ))
      .sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))
      .map((record) => AuditEventSchema.parse(record));
  }

  async upsertAlert(input: UpsertRiskAlertInput): Promise<RiskAlert> {
    const existingIndex = this.alerts.findIndex((record) => (
      record.tenantId === input.tenantId
      && record.kind === input.kind
      && record.customerId === input.customerId
      && record.employeeUserId === input.employeeUserId
    ));
    const existing = this.alerts[existingIndex];
    const record = RiskAlertSchema.parse({
      ...existing,
      ...input,
      id: existing?.id ?? input.id ?? this.createId(),
      version: existing ? existing.version + 1 : 1,
      createdAt: existing?.createdAt ?? input.createdAt ?? this.now(),
    });

    if (existingIndex >= 0) {
      this.alerts[existingIndex] = record;
    } else {
      this.alerts.push(record);
    }
    return RiskAlertSchema.parse(record);
  }

  async getOwnerDashboard(input: OwnerDashboardQuery): Promise<OwnerDashboardSource> {
    const filters = OwnerFilterSchema.parse({
      from: input.from,
      to: input.to,
      employeeUserId: input.employeeUserId,
      customerNumber: input.customerNumber,
      customerQuery: input.customerQuery,
      category: input.category,
      result: input.result,
    });
    const customers = this.customers.filter((customer) => (
        customer.tenantId === input.tenantId
        && (!filters.employeeUserId || customer.employeeUserId === filters.employeeUserId)
        && (!filters.customerNumber || customer.customerNumber === filters.customerNumber)
        && (!filters.customerQuery || ownerCustomerMatches(customer.customerAlias, customer.customerNumber, filters.customerQuery))
        && (!filters.category || customer.category === filters.category)
      ));
    const customersById = new Map(customers.map((customer) => [customer.id, customer]));
    const followUps = this.followUps.filter((record) => (
      record.tenantId === input.tenantId
      && customersById.has(record.customerId)
      && (!filters.from || new Date(record.createdAt).getTime() >= new Date(filters.from).getTime())
      && (!filters.to || new Date(record.createdAt).getTime() <= new Date(filters.to).getTime())
      && (!filters.result || record.result === filters.result)
    ));
    const records: OwnerDashboardRecord[] = followUps.map((followUp) => {
      const customer = customersById.get(followUp.customerId)!;
      const decisionUsages = this.decisionUsages.filter((usage) => (
        usage.tenantId === followUp.tenantId && usage.followUpId === followUp.id
      ));
      const reviewRequiredUsages = decisionUsages.filter(
        (usage) => usage.safetyStatus === "reviewRequired",
      );
      return {
        id: followUp.id,
        tenantId: followUp.tenantId,
        customerId: followUp.customerId,
        customerNumber: customer.customerNumber,
        customerAlias: customer.customerAlias,
        employeeUserId: followUp.employeeUserId,
        employeeLabel: followUp.employeeUserId,
        category: customer.category,
        submittedAt: followUp.createdAt,
        responseStartedAt: customer.createdAt,
        plannedFollowUpAt: followUp.plannedFollowUpAt,
        actualFollowUpAt: followUp.actualFollowUpAt,
        result: followUp.result,
        replyUsage: followUp.replyUsage,
        nextAction: followUp.nextAction,
        decisionCardId: followUp.decisionCardId,
        customerFingerprint: customer.customerFingerprint,
        safetyReviewRequired: reviewRequiredUsages.length > 0,
        safetyFlags: [...new Set(reviewRequiredUsages.flatMap(
          (usage) => usage.safetyRuleIds ?? [],
        ))].sort(),
      };
    });
    const alerts = this.alerts
      .filter((alert) => alert.tenantId === input.tenantId && alert.status === "open")
      .map((alert) => RiskAlertSchema.parse(alert));
    const duplicateCustomerSources = this.customers
      .filter((customer) => (
        customer.tenantId === input.tenantId && customer.customerFingerprint !== undefined
      ))
      .map((customer) => ({
        id: customer.id,
        tenantId: customer.tenantId,
        customerId: customer.id,
        employeeUserId: customer.employeeUserId,
        customerFingerprint: customer.customerFingerprint,
      }));
    const riskSignals = evaluateRiskAlerts(
      records,
      input.now,
      alerts,
      duplicateCustomerSources,
    );
    const recordsWithRisks = records.map((record) => ({
      ...record,
      riskLabels: riskLabelsForDashboardRecord(record, riskSignals),
    }));
    const aggregate = aggregateOwnerDashboard(recordsWithRisks, input.now);

    return {
      tenantId: input.tenantId,
      generatedAt: input.now,
      ...aggregate,
      riskSignals,
      alerts,
    };
  }

  async saveReplyFeedback(input: CreateReplyFeedbackInput): Promise<ReplyFeedbackSaveResult> {
    const parsed = CreateReplyFeedbackInputSchema.parse(input);
    const candidateId = this.createId();
    const candidateTimestamp = this.now();
    const existingIndex = this.replyFeedback.findIndex((record) => (
      this.replyFeedbackKey(record) === this.replyFeedbackKey(parsed)
    ));
    const existing = this.replyFeedback[existingIndex];

    if (existing && !this.sameReplyFeedbackSnapshot(existing, parsed)) {
      throw new Error("reply feedback immutable snapshot mismatch");
    }
    if (existing?.privacyStatus === "quarantined" && parsed.privacyStatus === "safe") {
      throw new Error("quarantined reply feedback requires guarded replacement");
    }

    if (existing && this.replyFeedbackContent(existing) === this.replyFeedbackContent(parsed)) {
      return {
        outcome: "unchanged",
        record: ReplyFeedbackRecordSchema.parse(existing),
      };
    }

    const timestamp = this.nextReplyFeedbackTimestamp(candidateTimestamp, existing?.updatedAt);
    const record = ReplyFeedbackRecordSchema.parse({
      ...parsed,
      id: existing?.id ?? candidateId,
      createdAt: existing?.createdAt ?? timestamp,
      updatedAt: timestamp,
    });

    if (!existing) {
      const audit = this.buildReplyFeedbackAudit(record, null);
      this.replyFeedback.push(record);
      this.audits.push(audit);
      return { outcome: "created", record: ReplyFeedbackRecordSchema.parse(record) };
    }

    const audit = this.buildReplyFeedbackAudit(record, existing.privacyStatus);
    this.replyFeedback[existingIndex] = record;
    this.audits.push(audit);
    return { outcome: "updated", record: ReplyFeedbackRecordSchema.parse(record) };
  }

  async replaceQuarantinedReplyFeedback(
    input: CreateReplyFeedbackInput,
  ): Promise<ReplyFeedbackSaveResult | undefined> {
    const parsed = CreateReplyFeedbackInputSchema.parse(input);
    if (parsed.privacyStatus !== "safe") return undefined;

    const existing = this.replyFeedback.find((record) => (
      this.replyFeedbackKey(record) === this.replyFeedbackKey(parsed)
    ));
    if (
      existing?.privacyStatus !== "quarantined"
      || !this.sameReplyFeedbackSnapshot(existing, parsed)
    ) {
      return undefined;
    }

    const existingIndex = this.replyFeedback.indexOf(existing);
    const record = ReplyFeedbackRecordSchema.parse({
      ...parsed,
      id: existing.id,
      createdAt: existing.createdAt,
      updatedAt: this.nextReplyFeedbackTimestamp(this.now(), existing.updatedAt),
    });
    const audit = this.buildReplyFeedbackAudit(record, existing.privacyStatus);
    this.replyFeedback[existingIndex] = record;
    this.audits.push(audit);
    return { outcome: "updated", record: ReplyFeedbackRecordSchema.parse(record) };
  }

  async listReplyFeedbackForOwner(
    input: OwnerReplyQualityQuery,
  ): Promise<ReplyFeedbackRecord[]> {
    return this.replyFeedback
      .filter((record) => (
        record.tenantId === input.tenantId
        && (!input.employeeUserId || record.employeeUserId === input.employeeUserId)
        && (!input.scenario || record.scenario === input.scenario)
        && (!input.salesStage || record.salesStage === input.salesStage)
        && (!input.agentVersion || record.agentVersion === input.agentVersion)
      ))
      .sort((left, right) => (
        right.updatedAt.localeCompare(left.updatedAt) || left.id.localeCompare(right.id)
      ))
      .map((record) => ReplyFeedbackRecordSchema.parse(record));
  }

  private requireCustomer(tenantId: string, customerId: string): CustomerRecord {
    const record = this.customers.find(
      (candidate) => candidate.tenantId === tenantId && candidate.id === customerId,
    );
    if (!record) {
      throw new Error("customer not found");
    }
    return CustomerRecordSchema.parse(record);
  }

  private replyFeedbackKey(
    input: Pick<
      CreateReplyFeedbackInput,
      "tenantId" | "employeeUserId" | "cardInstanceId" | "agentVersion"
    >,
  ): string {
    return [
      input.tenantId,
      input.employeeUserId,
      input.cardInstanceId,
      input.agentVersion,
    ].join("\u0000");
  }

  private replyFeedbackContent(
    input: CreateReplyFeedbackInput | ReplyFeedbackRecord,
  ): string {
    return JSON.stringify({
      tenantId: input.tenantId,
      employeeUserId: input.employeeUserId,
      customerId: input.customerId,
      decisionCardId: input.decisionCardId,
      cardInstanceId: input.cardInstanceId,
      cardDigest: input.cardDigest,
      agentVersion: input.agentVersion,
      rating: input.rating,
      reasonCodes: input.reasonCodes,
      scenario: input.scenario,
      salesStage: input.salesStage,
      evidenceIds: input.evidenceIds,
      privacyStatus: input.privacyStatus,
      privacyReasonCodes: input.privacyReasonCodes,
      sanitizedFinalReply: input.privacyStatus === "safe"
        ? input.sanitizedFinalReply
        : undefined,
    });
  }

  private sameReplyFeedbackSnapshot(
    existing: ReplyFeedbackRecord,
    replacement: CreateReplyFeedbackInput,
  ): boolean {
    return existing.customerId === replacement.customerId
      && existing.decisionCardId === replacement.decisionCardId
      && existing.cardInstanceId === replacement.cardInstanceId
      && existing.cardDigest === replacement.cardDigest
      && existing.scenario === replacement.scenario
      && existing.salesStage === replacement.salesStage
      && existing.evidenceIds.length === replacement.evidenceIds.length
      && existing.evidenceIds.every((id, index) => id === replacement.evidenceIds[index]);
  }

  private nextReplyFeedbackTimestamp(candidate: string, previous?: string): string {
    if (!previous || candidate > previous) return candidate;
    return new Date(new Date(previous).getTime() + 1).toISOString();
  }

  private buildReplyFeedbackAudit(
    record: ReplyFeedbackRecord,
    fromPrivacyStatus: ReplyFeedbackRecord["privacyStatus"] | null,
  ): AuditEvent {
    const action = fromPrivacyStatus === null
      ? "reply_feedback.created"
      : fromPrivacyStatus === "quarantined" && record.privacyStatus === "safe"
        ? "reply_feedback.quarantine_released"
        : "reply_feedback.updated";
    return AuditEventSchema.parse({
      id: `reply-feedback-audit-${record.id}-${record.updatedAt}`,
      tenantId: record.tenantId,
      actorUserId: record.employeeUserId,
      action,
      targetKind: "reply_feedback",
      targetId: record.id,
      occurredAt: record.updatedAt,
      details: {
        fromPrivacyStatus,
        toPrivacyStatus: record.privacyStatus,
        reasonCodes: [...record.reasonCodes],
        privacyReasonCodes: [...record.privacyReasonCodes],
      },
      version: 1,
      createdAt: record.updatedAt,
    });
  }

  private requireFollowUp(tenantId: string, followUpId: string): FollowUpRecord {
    const record = this.followUps.find(
      (candidate) => candidate.tenantId === tenantId && candidate.id === followUpId,
    );
    if (!record) {
      throw new Error("follow-up not found");
    }
    return FollowUpRecordSchema.parse(record);
  }

  private buildCustomer(input: CreateCustomerRecordInput): CustomerRecord {
    return CustomerRecordSchema.parse({
      ...input,
      id: input.id ?? this.createId(),
      version: 1,
      createdAt: input.createdAt ?? this.now(),
    });
  }

  private buildFollowUp(input: AppendFollowUpRecordInput): FollowUpRecord {
    return FollowUpRecordSchema.parse({
      ...input,
      id: input.id ?? this.createId(),
      version: 1,
      createdAt: input.createdAt ?? this.now(),
    });
  }

  private buildDecisionUsage(input: AppendDecisionUsageRecordInput): DecisionUsageRecord {
    return DecisionUsageRecordSchema.parse({
      ...input,
      id: input.id ?? this.createId(),
      version: 1,
      createdAt: input.createdAt ?? this.now(),
    });
  }

  private buildCorrection(
    input: AppendCorrectionRecordInput,
    followUp: FollowUpRecord,
  ): CorrectionRecord {
    return CorrectionRecordSchema.parse({
      ...input,
      id: input.id ?? this.createId(),
      customerId: followUp.customerId,
      version: 1,
      createdAt: input.createdAt ?? this.now(),
    });
  }

  private buildVaultAsset(input: CreateVaultAssetRecordInput): VaultAssetRecord {
    return VaultAssetRecordSchema.parse({
      ...input,
      id: input.id ?? this.createId(),
      version: 1,
      createdAt: input.createdAt ?? this.now(),
    });
  }

  private buildVaultPendingUpload(
    input: ReserveVaultUploadBundleInput["pendingUploads"][number],
  ): VaultPendingUploadRecord {
    return VaultPendingUploadRecordSchema.parse({
      ...input,
      id: input.id ?? this.createId(),
      version: 1,
      createdAt: input.createdAt ?? this.now(),
    });
  }

  private buildAudit(input: AppendAuditInput): AuditEvent {
    return AuditEventSchema.parse({
      ...input,
      id: input.id ?? this.createId(),
      version: 1,
      createdAt: input.createdAt ?? this.now(),
    });
  }

  private assertSameCustomer(
    customer: CustomerRecord,
    tenantId: string,
    customerId: string,
  ): void {
    if (customer.tenantId !== tenantId || customer.id !== customerId) {
      throw new Error("bundle customer mismatch");
    }
  }

  private assertAuditTarget(
    audit: AuditEvent,
    tenantId: string,
    targetKind: string,
    targetId: string,
  ): void {
    if (
      audit.tenantId !== tenantId
      || audit.targetKind !== targetKind
      || audit.targetId !== targetId
    ) {
      throw new Error("audit target mismatch");
    }
  }

  private assertAuditActor(audit: AuditEvent, actorUserId: string): void {
    if (audit.actorUserId !== actorUserId) throw new Error("audit actor mismatch");
  }

  private assertDecisionUsageMatches(
    followUp: FollowUpRecord,
    usage: DecisionUsageRecord,
  ): void {
    if (usage.followUpId !== followUp.id) throw new Error("decision usage follow-up mismatch");
    if (usage.employeeUserId !== followUp.employeeUserId) {
      throw new Error("decision usage employee mismatch");
    }
    if (
      usage.decisionCardId !== followUp.decisionCardId
      || usage.cardInstanceId !== followUp.cardInstanceId
      || usage.replyUsage !== followUp.replyUsage
      || usage.chosenReplyId !== followUp.chosenReplyId
      || usage.finalEditedText !== followUp.finalEditedText
    ) {
      throw new Error("decision usage selection mismatch");
    }
  }

  private assertUnique(
    records: readonly { tenantId: string; id: string }[],
    tenantId: string,
    id: string,
    label: string,
  ): void {
    if (records.some((record) => record.tenantId === tenantId && record.id === id)) {
      throw new Error(`${label} already exists`);
    }
  }

  private assertUniqueIdempotency(
    records: readonly {
      tenantId: string;
      idempotencyKey?: string;
      employeeUserId?: string;
      actorUserId?: string;
    }[],
    tenantId: string,
    actorUserId: string,
    idempotencyKey: string | undefined,
    label: string,
  ): void {
    if (!idempotencyKey) return;
    if (records.some((record) => (
      record.tenantId === tenantId
      && record.idempotencyKey === idempotencyKey
      && (record.employeeUserId ?? record.actorUserId) === actorUserId
    ))) {
      throw new Error(`${label} idempotency key already exists`);
    }
  }

}

export function seededMemorySupervisionRepository(): MemorySupervisionRepository {
  return new MemorySupervisionRepository({
    now: () => "2026-08-30T07:30:00.000Z",
    createId: () => "generated-correction-1",
    seed: {
      customers: [{
        id: "customer-a",
        tenantId: "tenant-a",
        employeeUserId: "employee-a",
        customerNumber: "C-001",
        customerAlias: "林女士",
        category: "沉香",
        plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
        status: "pending",
        version: 1,
        createdAt: "2026-08-30T06:30:00.000Z",
      }],
      followUps: [{
        id: "f-1",
        tenantId: "tenant-a",
        customerId: "customer-a",
        employeeUserId: "employee-a",
        customerMessageVaultAssetId: "vault-follow-up-a",
        customerMessageSummary: "客户希望确认到店时间。",
        plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
        result: "pending",
        nextAction: "明天电话确认。",
        decisionCardId: "card-001",
        replyUsage: "not_used",
        version: 1,
        createdAt: "2026-08-30T06:30:00.000Z",
      }],
    },
  });
}
