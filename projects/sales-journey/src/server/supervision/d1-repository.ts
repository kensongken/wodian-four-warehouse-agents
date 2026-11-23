import type {
  AuditEvent,
  CorrectionRecord,
  CustomerRecord,
  DecisionUsageRecord,
  FollowUpRecord,
  CreateReplyFeedbackInput,
  OwnerCustomerRiskRecord,
  OwnerDashboardRecord,
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
  AppendCorrectionBundleInput,
  AppendCorrectionBundleResult,
  AppendCorrectionRecordInput,
  AppendDecisionUsageRecordInput,
  AppendFollowUpBundleInput,
  AppendFollowUpBundleResult,
  AppendFollowUpRecordInput,
  ActorIdempotencyKey,
  CreateVaultUploadBundleInput,
  ReserveVaultUploadBundleInput,
  CreateCustomerBundleInput,
  CreateCustomerBundleResult,
  CreateCustomerRecordInput,
  CreateVaultAssetRecordInput,
  EmployeeIdempotencyKey,
  FinalizeVaultAssetDeletionInput,
  OwnerDashboardQuery,
  OwnerDashboardSource,
  SupervisionRepository,
  TenantCustomerKey,
  TenantAuditQuery,
  TenantEmployeeKey,
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
  OwnerDashboardRecordSchema,
  RiskAlertSchema,
  VaultAssetRecordSchema,
  VaultPendingUploadRecordSchema,
} from "./record-schemas";
import { normalizeExpiredVaultAssetLimit } from "./retention-limit";

export type D1RunResult = Readonly<{
  success: boolean;
  results?: readonly unknown[];
  meta?: Readonly<{ changes?: number }>;
}>;
type D1AllResult<T> = Readonly<{ results: T[]; success?: boolean }>;

export interface D1PreparedStatementLike {
  bind(...values: unknown[]): D1PreparedStatementLike;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<D1AllResult<T>>;
  run(): Promise<D1RunResult>;
}

export interface D1DatabaseLike {
  prepare(sql: string): D1PreparedStatementLike;
  batch?(statements: D1PreparedStatementLike[]): Promise<D1RunResult[]>;
}

type D1RepositoryOptions = Readonly<{
  now?: () => string;
  createId?: () => string;
}>;

type D1Row = Record<string, unknown>;

function defaultNow(): string {
  return new Date().toISOString();
}

function defaultCreateId(): string {
  return crypto.randomUUID();
}

function optionalString(value: unknown): unknown {
  return value === null || value === undefined ? undefined : value;
}

function requiredString(value: unknown): unknown {
  return value;
}

function requiredNumber(value: unknown): unknown {
  return value;
}

function optionalNumber(value: unknown): unknown {
  return value === null || value === undefined ? undefined : value;
}

function parseStringArray(value: unknown): unknown {
  if (typeof value !== "string") return value;
  return JSON.parse(value) as unknown;
}

function parseJsonObject(value: unknown): unknown {
  if (typeof value !== "string") return value === null ? undefined : value;
  return JSON.parse(value) as unknown;
}

function mapCustomer(row: D1Row): CustomerRecord {
  return CustomerRecordSchema.parse({
    id: requiredString(row.id),
    tenantId: requiredString(row.tenant_id),
    employeeUserId: requiredString(row.employee_user_id),
    idempotencyKey: optionalString(row.idempotency_key),
    requestFingerprint: optionalString(row.request_fingerprint),
    customerFingerprint: optionalString(row.customer_fingerprint),
    customerNumber: requiredString(row.customer_number),
    customerAlias: requiredString(row.customer_alias),
    category: requiredString(row.category),
    plannedFollowUpAt: requiredString(row.planned_follow_up_at),
    status: requiredString(row.status),
    version: requiredNumber(row.version),
    createdAt: requiredString(row.created_at),
  });
}

function mapFollowUp(row: D1Row): FollowUpRecord {
  return FollowUpRecordSchema.parse({
    id: requiredString(row.id),
    tenantId: requiredString(row.tenant_id),
    customerId: requiredString(row.customer_id),
    employeeUserId: requiredString(row.employee_user_id),
    idempotencyKey: optionalString(row.idempotency_key),
    requestFingerprint: optionalString(row.request_fingerprint),
    customerMessageVaultAssetId: requiredString(row.customer_message_vault_asset_id),
    customerMessageSummary: requiredString(row.customer_message_summary),
    plannedFollowUpAt: requiredString(row.planned_follow_up_at),
    actualFollowUpAt: optionalString(row.actual_follow_up_at),
    result: requiredString(row.result),
    nextAction: requiredString(row.next_action),
    decisionCardId: requiredString(row.decision_card_id),
    cardInstanceId: optionalString(row.card_instance_id),
    replyUsage: requiredString(row.reply_usage),
    chosenReplyId: optionalString(row.chosen_reply_id),
    finalEditedText: optionalString(row.final_edited_text),
    version: requiredNumber(row.version),
    createdAt: requiredString(row.created_at),
  });
}

function mapDecisionUsage(row: D1Row): DecisionUsageRecord {
  return DecisionUsageRecordSchema.parse({
    id: requiredString(row.id),
    tenantId: requiredString(row.tenant_id),
    customerId: requiredString(row.customer_id),
    followUpId: requiredString(row.follow_up_id),
    employeeUserId: requiredString(row.employee_user_id),
    decisionCardId: requiredString(row.decision_card_id),
    cardInstanceId: optionalString(row.card_instance_id),
    cardDigest: optionalString(row.card_digest),
    scenario: requiredString(row.scenario),
    deliveryMode: requiredString(row.delivery_mode),
    providedReplyIds: parseStringArray(row.provided_reply_ids_json),
    replyUsage: requiredString(row.reply_usage),
    chosenReplyId: optionalString(row.chosen_reply_id),
    finalEditedText: optionalString(row.final_edited_text),
    copiedAt: optionalString(row.copied_at),
    safetyStatus: optionalString(row.safety_status),
    safetyRuleIds: row.safety_rule_ids_json === null || row.safety_rule_ids_json === undefined
      ? undefined
      : parseStringArray(row.safety_rule_ids_json),
    version: requiredNumber(row.version),
    createdAt: requiredString(row.created_at),
  });
}

function mapCorrection(row: D1Row): CorrectionRecord {
  return CorrectionRecordSchema.parse({
    id: requiredString(row.id),
    tenantId: requiredString(row.tenant_id),
    customerId: requiredString(row.customer_id),
    followUpId: requiredString(row.follow_up_id),
    actorUserId: requiredString(row.actor_user_id),
    idempotencyKey: optionalString(row.idempotency_key),
    requestFingerprint: optionalString(row.request_fingerprint),
    reason: requiredString(row.reason),
    version: requiredNumber(row.version),
    createdAt: requiredString(row.created_at),
  });
}

function mapVaultAsset(row: D1Row): VaultAssetRecord {
  return VaultAssetRecordSchema.parse({
    id: requiredString(row.id),
    tenantId: requiredString(row.tenant_id),
    customerId: requiredString(row.customer_id),
    uploadedByUserId: requiredString(row.uploaded_by_user_id),
    kind: requiredString(row.kind),
    vaultKey: requiredString(row.vault_key),
    byteSize: optionalNumber(row.byte_size),
    uploadedAt: requiredString(row.uploaded_at),
    deleteAfter: requiredString(row.delete_after),
    deletedAt: optionalString(row.deleted_at),
    deletedByUserId: optionalString(row.deleted_by_user_id),
    version: requiredNumber(row.version),
    createdAt: requiredString(row.created_at),
  });
}

function mapVaultPendingUpload(row: D1Row): VaultPendingUploadRecord {
  return VaultPendingUploadRecordSchema.parse({
    id: requiredString(row.id),
    tenantId: requiredString(row.tenant_id),
    customerId: requiredString(row.customer_id),
    uploadedByUserId: requiredString(row.uploaded_by_user_id),
    vaultKey: requiredString(row.vault_key),
    byteSize: requiredNumber(row.byte_size),
    uploadedAt: requiredString(row.uploaded_at),
    deleteAfter: requiredString(row.delete_after),
    uploadDay: requiredString(row.upload_day),
    version: requiredNumber(row.version),
    createdAt: requiredString(row.created_at),
  });
}

function mapAudit(row: D1Row): AuditEvent {
  return AuditEventSchema.parse({
    id: requiredString(row.id),
    tenantId: requiredString(row.tenant_id),
    actorUserId: row.actor_user_id === null ? null : requiredString(row.actor_user_id),
    action: requiredString(row.action),
    targetKind: requiredString(row.target_kind),
    targetId: requiredString(row.target_id),
    occurredAt: requiredString(row.occurred_at),
    details: parseJsonObject(row.details_json),
    version: requiredNumber(row.version),
    createdAt: requiredString(row.created_at),
  });
}

function mapAlert(row: D1Row): RiskAlert {
  return RiskAlertSchema.parse({
    id: requiredString(row.id),
    tenantId: requiredString(row.tenant_id),
    customerId: optionalString(row.customer_id),
    employeeUserId: optionalString(row.employee_user_id),
    detectedByUserId: row.detected_by_user_id === null
      ? null
      : requiredString(row.detected_by_user_id),
    kind: requiredString(row.kind),
    status: requiredString(row.status),
    resolvedAt: optionalString(row.resolved_at),
    resolvedByUserId: optionalString(row.resolved_by_user_id),
    resolutionNote: optionalString(row.resolution_note),
    version: requiredNumber(row.version),
    createdAt: requiredString(row.created_at),
  });
}

function mapReplyFeedback(row: D1Row): ReplyFeedbackRecord {
  const privacyStatus = requiredString(row.privacy_status);
  const record = {
    id: requiredString(row.id),
    tenantId: requiredString(row.tenant_id),
    employeeUserId: requiredString(row.employee_user_id),
    customerId: requiredString(row.customer_id),
    decisionCardId: requiredString(row.decision_card_id),
    cardInstanceId: requiredString(row.card_instance_id),
    cardDigest: requiredString(row.card_digest),
    agentVersion: requiredString(row.agent_version),
    rating: requiredString(row.rating),
    reasonCodes: parseStringArray(row.reason_codes_json),
    scenario: requiredString(row.scenario),
    salesStage: requiredString(row.sales_stage),
    evidenceIds: parseStringArray(row.evidence_ids_json),
    privacyStatus,
    privacyReasonCodes: parseStringArray(row.privacy_reason_codes_json),
    createdAt: requiredString(row.created_at),
    updatedAt: requiredString(row.updated_at),
    ...(privacyStatus === "safe" && row.sanitized_final_reply !== null
      && row.sanitized_final_reply !== undefined
      ? { sanitizedFinalReply: requiredString(row.sanitized_final_reply) }
      : {}),
  };
  return ReplyFeedbackRecordSchema.parse(record);
}

function sameReplyFeedbackSnapshot(
  existing: ReplyFeedbackRecord,
  input: CreateReplyFeedbackInput,
): boolean {
  return existing.tenantId === input.tenantId
    && existing.employeeUserId === input.employeeUserId
    && existing.customerId === input.customerId
    && existing.decisionCardId === input.decisionCardId
    && existing.cardInstanceId === input.cardInstanceId
    && existing.cardDigest === input.cardDigest
    && existing.agentVersion === input.agentVersion
    && existing.scenario === input.scenario
    && existing.salesStage === input.salesStage
    && existing.evidenceIds.length === input.evidenceIds.length
    && existing.evidenceIds.every((id, index) => id === input.evidenceIds[index]);
}

function sameReplyFeedbackEvaluation(
  existing: ReplyFeedbackRecord,
  input: CreateReplyFeedbackInput,
): boolean {
  return existing.rating === input.rating
    && existing.reasonCodes.length === input.reasonCodes.length
    && existing.reasonCodes.every((code, index) => code === input.reasonCodes[index])
    && existing.privacyStatus === input.privacyStatus
    && existing.privacyReasonCodes.length === input.privacyReasonCodes.length
    && existing.privacyReasonCodes.every(
      (code, index) => code === input.privacyReasonCodes[index],
    )
    && (existing.privacyStatus !== "safe" || input.privacyStatus !== "safe"
      || existing.sanitizedFinalReply === input.sanitizedFinalReply);
}

function batchResultRow(result: D1RunResult | undefined): D1Row | undefined {
  const row = result?.results?.[0];
  if (row === undefined) return undefined;
  if (typeof row !== "object" || row === null || Array.isArray(row)) {
    throw new Error("invalid D1 reply feedback batch row");
  }
  return row as D1Row;
}

function mapOwnerDashboardRecord(row: D1Row): OwnerDashboardRecord {
  const followUp = mapFollowUp(row);
  const parsedSafetyFlags = row.safety_rule_ids_json === null
    || row.safety_rule_ids_json === undefined
    ? []
    : parseStringArray(row.safety_rule_ids_json);
  if (!Array.isArray(parsedSafetyFlags) || parsedSafetyFlags.some((flag) => typeof flag !== "string")) {
    throw new Error("invalid dashboard safety flags");
  }
  const safetyStatus = optionalString(row.safety_status);
  if (
    safetyStatus !== undefined
    && safetyStatus !== "safe"
    && safetyStatus !== "reviewRequired"
  ) {
    throw new Error("invalid dashboard safety status");
  }
  return OwnerDashboardRecordSchema.parse({
    id: followUp.id,
    tenantId: followUp.tenantId,
    customerId: followUp.customerId,
    customerNumber: requiredString(row.customer_number),
    customerAlias: requiredString(row.customer_alias),
    employeeUserId: followUp.employeeUserId,
    employeeLabel: requiredString(row.employee_label),
    category: requiredString(row.category),
    submittedAt: followUp.createdAt,
    responseStartedAt: requiredString(row.response_started_at),
    plannedFollowUpAt: followUp.plannedFollowUpAt,
    actualFollowUpAt: followUp.actualFollowUpAt,
    result: followUp.result,
    replyUsage: followUp.replyUsage,
    nextAction: followUp.nextAction,
    decisionCardId: followUp.decisionCardId,
    customerFingerprint: optionalString(row.customer_fingerprint),
    safetyReviewRequired: safetyStatus === "reviewRequired",
    safetyFlags: safetyStatus === "reviewRequired" ? parsedSafetyFlags : [],
  });
}

function mergeOwnerDashboardRecords(rows: readonly D1Row[]): OwnerDashboardRecord[] {
  const records = new Map<string, OwnerDashboardRecord>();
  for (const row of rows) {
    const next = mapOwnerDashboardRecord(row);
    const current = records.get(next.id);
    if (!current) {
      records.set(next.id, next);
      continue;
    }
    records.set(next.id, OwnerDashboardRecordSchema.parse({
      ...current,
      safetyReviewRequired: current.safetyReviewRequired || next.safetyReviewRequired,
      safetyFlags: [...new Set([
        ...(current.safetyFlags ?? []),
        ...(next.safetyFlags ?? []),
      ])].sort(),
    }));
  }
  return [...records.values()];
}

function mapOwnerCustomerRiskRecord(row: D1Row): OwnerCustomerRiskRecord {
  const id = row.id;
  const tenantId = row.tenant_id;
  const employeeUserId = row.employee_user_id;
  const customerFingerprint = row.customer_fingerprint;
  if (
    typeof id !== "string"
    || id.trim().length === 0
    || typeof tenantId !== "string"
    || tenantId.trim().length === 0
    || typeof employeeUserId !== "string"
    || employeeUserId.trim().length === 0
    || typeof customerFingerprint !== "string"
    || !/^fp_[a-f0-9]{64}$/u.test(customerFingerprint)
  ) {
    throw new Error("invalid duplicate-customer source row");
  }
  return {
    id,
    tenantId,
    customerId: id,
    employeeUserId,
    customerFingerprint,
  };
}

export class D1SupervisionRepository implements SupervisionRepository {
  private readonly now: () => string;
  private readonly createId: () => string;

  constructor(
    private readonly d1: D1DatabaseLike,
    options: D1RepositoryOptions = {},
  ) {
    this.now = options.now ?? defaultNow;
    this.createId = options.createId ?? defaultCreateId;
  }

  async createCustomerBundle(
    input: CreateCustomerBundleInput,
  ): Promise<CreateCustomerBundleResult> {
    const customer = this.buildCustomer(input.customer);
    const audit = this.buildAudit(input.audit);
    const vaultAsset = input.vaultAsset ? this.buildVaultAsset(input.vaultAsset) : undefined;
    if (vaultAsset) {
      this.assertSameCustomer(customer, vaultAsset.tenantId, vaultAsset.customerId);
      if (vaultAsset.uploadedByUserId !== customer.employeeUserId) {
        throw new Error("vault uploader mismatch");
      }
    }
    this.assertAuditTarget(audit, customer.tenantId, "customer", customer.id);
    this.assertAuditActor(audit, customer.employeeUserId);

    const statements = [this.prepareCustomerInsert(customer)];
    if (vaultAsset) statements.push(this.prepareVaultAssetInsert(vaultAsset));
    statements.push(this.prepareAuditInsert(audit));
    await this.runBatch(statements);
    return { customer, vaultAsset, audit };
  }

  async getCustomerBundleByIdempotencyKey(
    input: EmployeeIdempotencyKey,
  ): Promise<CreateCustomerBundleResult | undefined> {
    const customerRow = await this.d1.prepare(
      `SELECT * FROM customers
       WHERE tenant_id = ? AND employee_user_id = ? AND idempotency_key = ? LIMIT 1`,
    ).bind(input.tenantId, input.employeeUserId, input.idempotencyKey).first<D1Row>();
    if (!customerRow) return undefined;
    const customer = mapCustomer(customerRow);
    const auditRow = await this.d1.prepare(
      `SELECT * FROM audit_events
       WHERE tenant_id = ? AND target_kind = ? AND target_id = ? LIMIT 1`,
    ).bind(input.tenantId, "customer", customer.id).first<D1Row>();
    if (!auditRow) throw new Error("customer audit not found");
    const vaultRow = await this.d1.prepare(
      `SELECT * FROM vault_assets
       WHERE tenant_id = ? AND customer_id = ? AND kind = ? LIMIT 1`,
    ).bind(input.tenantId, customer.id, "customer_identity").first<D1Row>();
    return {
      customer,
      vaultAsset: vaultRow ? mapVaultAsset(vaultRow) : undefined,
      audit: mapAudit(auditRow),
    };
  }

  async appendFollowUpBundle(
    input: AppendFollowUpBundleInput,
  ): Promise<AppendFollowUpBundleResult> {
    const customer = await this.getCustomer({
      tenantId: input.followUp.tenantId,
      customerId: input.followUp.customerId,
    });
    if (!customer) throw new Error("customer not found");
    const followUp = this.buildFollowUp(input.followUp);
    const decisionUsage = this.buildDecisionUsage(input.decisionUsage);
    const vaultAsset = this.buildVaultAsset(input.vaultAsset);
    const audit = this.buildAudit(input.audit);
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

    await this.runBatch([
      this.prepareVaultAssetInsert(vaultAsset),
      this.prepareFollowUpInsert(followUp),
      this.prepareDecisionUsageInsert(decisionUsage),
      this.prepareAuditInsert(audit),
    ]);
    return { followUp, decisionUsage, vaultAsset, audit };
  }

  async getFollowUpBundleByIdempotencyKey(
    input: EmployeeIdempotencyKey,
  ): Promise<AppendFollowUpBundleResult | undefined> {
    const followUpRow = await this.d1.prepare(
      `SELECT * FROM follow_ups
       WHERE tenant_id = ? AND employee_user_id = ? AND idempotency_key = ? LIMIT 1`,
    ).bind(input.tenantId, input.employeeUserId, input.idempotencyKey).first<D1Row>();
    if (!followUpRow) return undefined;
    const followUp = mapFollowUp(followUpRow);
    const [usageRow, vaultRow, auditRow] = await Promise.all([
      this.d1.prepare(
        "SELECT * FROM decision_usages WHERE tenant_id = ? AND follow_up_id = ? LIMIT 1",
      ).bind(input.tenantId, followUp.id).first<D1Row>(),
      this.d1.prepare(
        "SELECT * FROM vault_assets WHERE tenant_id = ? AND id = ? LIMIT 1",
      ).bind(input.tenantId, followUp.customerMessageVaultAssetId).first<D1Row>(),
      this.d1.prepare(
        `SELECT * FROM audit_events
         WHERE tenant_id = ? AND target_kind = ? AND target_id = ? LIMIT 1`,
      ).bind(input.tenantId, "follow_up", followUp.id).first<D1Row>(),
    ]);
    if (!usageRow || !vaultRow || !auditRow) throw new Error("follow-up receipt incomplete");
    return {
      followUp,
      decisionUsage: mapDecisionUsage(usageRow),
      vaultAsset: mapVaultAsset(vaultRow),
      audit: mapAudit(auditRow),
    };
  }

  async appendCorrectionBundle(
    input: AppendCorrectionBundleInput,
  ): Promise<AppendCorrectionBundleResult> {
    const followUpRow = await this.d1.prepare(
      "SELECT * FROM follow_ups WHERE tenant_id = ? AND id = ? LIMIT 1",
    ).bind(input.correction.tenantId, input.correction.followUpId).first<D1Row>();
    if (!followUpRow) throw new Error("follow-up not found");
    const correction = this.buildCorrection(input.correction, mapFollowUp(followUpRow));
    const audit = this.buildAudit(input.audit);
    this.assertAuditTarget(audit, correction.tenantId, "correction", correction.id);
    this.assertAuditActor(audit, correction.actorUserId);

    await this.runBatch([
      this.prepareCorrectionInsert(correction),
      this.prepareAuditInsert(audit),
    ]);
    return { correction, audit };
  }

  async getCorrectionBundleByIdempotencyKey(
    input: ActorIdempotencyKey,
  ): Promise<AppendCorrectionBundleResult | undefined> {
    const correctionRow = await this.d1.prepare(
      `SELECT * FROM corrections
       WHERE tenant_id = ? AND actor_user_id = ? AND idempotency_key = ? LIMIT 1`,
    ).bind(input.tenantId, input.actorUserId, input.idempotencyKey).first<D1Row>();
    if (!correctionRow) return undefined;
    const correction = mapCorrection(correctionRow);
    const auditRow = await this.d1.prepare(
      `SELECT * FROM audit_events
       WHERE tenant_id = ? AND target_kind = ? AND target_id = ? LIMIT 1`,
    ).bind(input.tenantId, "correction", correction.id).first<D1Row>();
    if (!auditRow) throw new Error("correction audit not found");
    return { correction, audit: mapAudit(auditRow) };
  }

  async createCustomer(input: CreateCustomerRecordInput): Promise<CustomerRecord> {
    const record = this.buildCustomer(input);
    await this.runStatement(this.prepareCustomerInsert(record));
    return CustomerRecordSchema.parse(record);
  }

  async getCustomer(input: TenantCustomerKey): Promise<CustomerRecord | undefined> {
    const row = await this.d1.prepare(
      "SELECT * FROM customers WHERE tenant_id = ? AND id = ? LIMIT 1",
    ).bind(input.tenantId, input.customerId).first<D1Row>();
    return row ? mapCustomer(row) : undefined;
  }

  async listEmployeeCustomers(input: TenantEmployeeKey): Promise<CustomerRecord[]> {
    const { results } = await this.d1.prepare(
      `SELECT * FROM customers
       WHERE tenant_id = ? AND employee_user_id = ?
       ORDER BY created_at DESC`,
    ).bind(input.tenantId, input.employeeUserId).all<D1Row>();
    return results.map(mapCustomer);
  }

  async appendFollowUp(input: AppendFollowUpRecordInput): Promise<FollowUpRecord> {
    const record = this.buildFollowUp(input);
    await this.runStatement(this.prepareFollowUpInsert(record));
    return FollowUpRecordSchema.parse(record);
  }

  async appendDecisionUsage(
    input: AppendDecisionUsageRecordInput,
  ): Promise<DecisionUsageRecord> {
    const followUpRow = await this.d1.prepare(
      "SELECT * FROM follow_ups WHERE tenant_id = ? AND id = ? LIMIT 1",
    ).bind(input.tenantId, input.followUpId).first<D1Row>();
    if (!followUpRow) throw new Error("follow-up not found");
    const followUp = mapFollowUp(followUpRow);
    if (followUp.customerId !== input.customerId) {
      throw new Error("follow-up customer mismatch");
    }
    const record = this.buildDecisionUsage(input);
    await this.runStatement(this.prepareDecisionUsageInsert(record));
    return DecisionUsageRecordSchema.parse(record);
  }

  async appendCorrection(input: AppendCorrectionRecordInput): Promise<CorrectionRecord> {
    const followUpRow = await this.d1.prepare(
      "SELECT * FROM follow_ups WHERE tenant_id = ? AND id = ? LIMIT 1",
    ).bind(input.tenantId, input.followUpId).first<D1Row>();
    if (!followUpRow) throw new Error("follow-up not found");
    const followUp = mapFollowUp(followUpRow);
    const record = this.buildCorrection(input, followUp);
    await this.runStatement(this.prepareCorrectionInsert(record));
    return CorrectionRecordSchema.parse(record);
  }

  async listTimeline(input: TenantCustomerKey): Promise<TimelineEntry[]> {
    const followUps = await this.d1.prepare(
      `SELECT * FROM follow_ups
       WHERE tenant_id = ? AND customer_id = ? ORDER BY created_at ASC`,
    ).bind(input.tenantId, input.customerId).all<D1Row>();
    const decisionUsages = await this.d1.prepare(
      `SELECT * FROM decision_usages
       WHERE tenant_id = ? AND customer_id = ? ORDER BY created_at ASC`,
    ).bind(input.tenantId, input.customerId).all<D1Row>();
    const corrections = await this.d1.prepare(
      `SELECT * FROM corrections
       WHERE tenant_id = ? AND customer_id = ? ORDER BY created_at ASC`,
    ).bind(input.tenantId, input.customerId).all<D1Row>();
    const entries: TimelineEntry[] = [
      ...followUps.results.map((row): TimelineEntry => ({ kind: "follow_up", record: mapFollowUp(row) })),
      ...decisionUsages.results.map((row): TimelineEntry => ({
        kind: "decision_usage",
        record: mapDecisionUsage(row),
      })),
      ...corrections.results.map((row): TimelineEntry => ({ kind: "correction", record: mapCorrection(row) })),
    ];
    return entries.sort((left, right) => left.record.createdAt.localeCompare(right.record.createdAt));
  }

  async createVaultAsset(input: CreateVaultAssetRecordInput): Promise<VaultAssetRecord> {
    const record = this.buildVaultAsset(input);
    await this.runStatement(this.prepareVaultAssetInsert(record));
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
    for (const upload of pendingUploads) {
      const customer = await this.getCustomer({
        tenantId: upload.tenantId,
        customerId: upload.customerId,
      });
      if (
        upload.tenantId !== tenantId
        || upload.uploadDay !== uploadDay
        || !customer
        || customer.employeeUserId !== upload.uploadedByUserId
      ) {
        throw new Error("vault upload reservation actor mismatch");
      }
    }
    const candidateBytes = pendingUploads.reduce((total, upload) => total + upload.byteSize, 0);
    const quotaStatement = this.d1.prepare(
      `INSERT INTO vault_upload_daily_quotas (
        tenant_id, upload_day, used_bytes, byte_limit, version, created_at
      ) VALUES (?, ?, ?, ?, 1, ?)
      ON CONFLICT (tenant_id, upload_day) DO UPDATE SET
        used_bytes = vault_upload_daily_quotas.used_bytes + excluded.used_bytes,
        byte_limit = excluded.byte_limit,
        version = vault_upload_daily_quotas.version + 1`,
    ).bind(
      tenantId,
      uploadDay,
      candidateBytes,
      input.dailyByteLimit,
      pendingUploads[0]!.createdAt,
    );

    try {
      await this.runBatch([
        quotaStatement,
        ...pendingUploads.map((upload) => this.prepareVaultPendingUploadInsert(upload)),
      ]);
      return pendingUploads;
    } catch (error) {
      const recovered = await Promise.all(pendingUploads.map((upload) => (
        this.getVaultPendingUpload({ tenantId: upload.tenantId, pendingUploadId: upload.id })
      )));
      if (recovered.every((record, index) => (
        record?.vaultKey === pendingUploads[index]!.vaultKey
        && record.byteSize === pendingUploads[index]!.byteSize
      ))) {
        return recovered as VaultPendingUploadRecord[];
      }
      const usedBytes = await this.getVaultDailyUploadUsage({ tenantId, uploadDay });
      if (usedBytes + candidateBytes > input.dailyByteLimit) {
        throw new VaultDailyQuotaExceededError();
      }
      throw error;
    }
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
    for (let index = 0; index < assets.length; index += 1) {
      const asset = assets[index]!;
      const audit = audits[index]!;
      const customer = await this.getCustomer({
        tenantId: asset.tenantId,
        customerId: asset.customerId,
      });
      if (
        asset.tenantId !== tenantId
        || !customer
        || customer.employeeUserId !== asset.uploadedByUserId
      ) {
        throw new Error("vault upload actor mismatch");
      }
      this.assertAuditTarget(audit, asset.tenantId, "vault_asset", asset.id);
      this.assertAuditActor(audit, asset.uploadedByUserId);
      const pending = await this.getVaultPendingUpload({
        tenantId: asset.tenantId,
        pendingUploadId: asset.id,
      });
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
    await this.runBatch(assets.flatMap((asset, index) => [
      this.prepareVaultAssetInsert(asset),
      this.prepareAuditInsert(audits[index]!),
      this.d1.prepare(
        "DELETE FROM vault_pending_uploads WHERE tenant_id = ? AND id = ?",
      ).bind(asset.tenantId, asset.id),
    ]));
    return assets.map((record) => VaultAssetRecordSchema.parse(record));
  }

  async getVaultAsset(input: TenantVaultKey): Promise<VaultAssetRecord | undefined> {
    const row = await this.d1.prepare(
      "SELECT * FROM vault_assets WHERE tenant_id = ? AND id = ? LIMIT 1",
    ).bind(input.tenantId, input.vaultAssetId).first<D1Row>();
    return row ? mapVaultAsset(row) : undefined;
  }

  async getVaultPendingUpload(
    input: TenantPendingVaultKey,
  ): Promise<VaultPendingUploadRecord | undefined> {
    const row = await this.d1.prepare(
      "SELECT * FROM vault_pending_uploads WHERE tenant_id = ? AND id = ? LIMIT 1",
    ).bind(input.tenantId, input.pendingUploadId).first<D1Row>();
    return row ? mapVaultPendingUpload(row) : undefined;
  }

  async getVaultDailyUploadUsage(input: TenantVaultUploadDayKey): Promise<number> {
    const row = await this.d1.prepare(
      `SELECT used_bytes FROM vault_upload_daily_quotas
       WHERE tenant_id = ? AND upload_day = ? LIMIT 1`,
    ).bind(input.tenantId, input.uploadDay).first<{ used_bytes: unknown }>();
    if (!row) return 0;
    const usedBytes = Number(row.used_bytes);
    if (!Number.isSafeInteger(usedBytes) || usedBytes < 0) {
      throw new Error("invalid vault daily quota usage");
    }
    return usedBytes;
  }

  async resolveVaultPendingUpload(input: TenantPendingVaultKey): Promise<void> {
    const result = await this.d1.prepare(
      "DELETE FROM vault_pending_uploads WHERE tenant_id = ? AND id = ?",
    ).bind(input.tenantId, input.pendingUploadId).run();
    if (!result.success) throw new Error("pending vault upload resolution failed");
  }

  async listVaultTenantIds(): Promise<string[]> {
    const { results } = await this.d1.prepare(
      `SELECT tenant_id FROM vault_assets
       UNION SELECT tenant_id FROM vault_pending_uploads
       ORDER BY tenant_id ASC`,
    ).all<{ tenant_id: unknown }>();
    return results.map((row) => String(row.tenant_id));
  }

  async listExpiredVaultAssets(input: {
    tenantId: string;
    now: string;
    limit: number;
    after?: VaultExpiryCursor;
  }): Promise<VaultAssetRecord[]> {
    const cursorClause = input.after
      ? "AND (delete_after > ? OR (delete_after = ? AND id > ?))"
      : "";
    const cursorBindings = input.after
      ? [input.after.deleteAfter, input.after.deleteAfter, input.after.id]
      : [];
    const { results } = await this.d1.prepare(
      `SELECT * FROM vault_assets
       WHERE tenant_id = ? AND deleted_at IS NULL AND delete_after <= ?
       ${cursorClause}
       ORDER BY delete_after ASC, id ASC LIMIT ?`,
    ).bind(
      input.tenantId,
      input.now,
      ...cursorBindings,
      normalizeExpiredVaultAssetLimit(input.limit),
    ).all<D1Row>();
    return results.map(mapVaultAsset);
  }

  async listExpiredVaultPendingUploads(input: {
    tenantId: string;
    now: string;
    limit: number;
    after?: VaultExpiryCursor;
  }): Promise<VaultPendingUploadRecord[]> {
    const cursorClause = input.after
      ? "AND (delete_after > ? OR (delete_after = ? AND id > ?))"
      : "";
    const cursorBindings = input.after
      ? [input.after.deleteAfter, input.after.deleteAfter, input.after.id]
      : [];
    const { results } = await this.d1.prepare(
      `SELECT * FROM vault_pending_uploads
       WHERE tenant_id = ? AND delete_after <= ?
       ${cursorClause}
       ORDER BY delete_after ASC, id ASC LIMIT ?`,
    ).bind(
      input.tenantId,
      input.now,
      ...cursorBindings,
      normalizeExpiredVaultAssetLimit(input.limit),
    ).all<D1Row>();
    return results.map(mapVaultPendingUpload);
  }

  async markVaultAssetDeleted(
    input: TenantVaultKey & { deletedAt: string },
  ): Promise<void> {
    const existingRow = await this.d1.prepare(
      "SELECT * FROM vault_assets WHERE tenant_id = ? AND id = ? LIMIT 1",
    ).bind(input.tenantId, input.vaultAssetId).first<D1Row>();
    if (!existingRow) throw new Error("vault asset not found");
    const existing = mapVaultAsset(existingRow);
    const updated = VaultAssetRecordSchema.parse({
      ...existing,
      deletedAt: input.deletedAt,
      version: existing.version + 1,
    });
    const result = await this.d1.prepare(
      `UPDATE vault_assets
       SET deleted_at = ?, version = ?
       WHERE tenant_id = ? AND id = ? AND version = ?`,
    ).bind(
      updated.deletedAt,
      updated.version,
      updated.tenantId,
      updated.id,
      existing.version,
    ).run();
    if (!result.success || result.meta?.changes !== 1) {
      throw new Error("vault asset update failed");
    }
  }

  async finalizeVaultAssetDeletion(input: FinalizeVaultAssetDeletionInput): Promise<void> {
    const existing = await this.getVaultAsset(input);
    if (!existing) throw new Error("vault asset not found");
    if (existing.deletedAt !== undefined) return;
    const audit = this.buildAudit({
      ...input.audit,
      id: `vault-deleted-${input.vaultAssetId}`,
    });
    this.assertAuditTarget(audit, input.tenantId, "vault_asset", input.vaultAssetId);
    if (audit.actorUserId !== null || audit.action !== "vault.deleted") {
      throw new Error("invalid retention audit");
    }
    const updated = VaultAssetRecordSchema.parse({
      ...existing,
      vaultKey: `deleted/${existing.id}`,
      deletedAt: input.deletedAt,
      version: existing.version + 1,
    });
    if (!this.d1.batch) throw new Error("D1 atomic batch binding unavailable");
    const statements = [
      this.d1.prepare(
        "DELETE FROM customer_vault_identities WHERE tenant_id = ? AND vault_asset_id = ?",
      ).bind(existing.tenantId, existing.id),
      this.d1.prepare(
        `UPDATE vault_assets
         SET vault_key = ?, deleted_at = ?, version = ?
         WHERE tenant_id = ? AND id = ? AND deleted_at IS NULL AND version = ?`,
      ).bind(
        updated.vaultKey,
        updated.deletedAt,
        updated.version,
        existing.tenantId,
        existing.id,
        existing.version,
      ),
      this.prepareAuditInsert(audit),
    ];
    const results = await this.d1.batch(statements);
    if (
      results.length !== statements.length
      || results.some((result) => !result.success)
      || results[1]?.meta?.changes !== 1
    ) {
      throw new Error("vault deletion finalization failed");
    }
  }

  async appendAudit(input: AppendAuditInput): Promise<AuditEvent> {
    const record = this.buildAudit(input);
    await this.runStatement(this.prepareAuditInsert(record));
    return AuditEventSchema.parse(record);
  }

  async listAuditEvents(input: TenantAuditQuery): Promise<AuditEvent[]> {
    const { results } = await this.d1.prepare(
      `SELECT * FROM audit_events
       WHERE tenant_id = ? AND target_kind = ? AND target_id = ?
       ORDER BY occurred_at ASC`,
    ).bind(input.tenantId, input.targetKind, input.targetId).all<D1Row>();
    return results.map(mapAudit);
  }

  async upsertAlert(input: UpsertRiskAlertInput): Promise<RiskAlert> {
    const record = RiskAlertSchema.parse({
      ...input,
      id: input.id ?? this.createId(),
      version: 1,
      createdAt: input.createdAt ?? this.now(),
    });
    const row = await this.d1.prepare(
      `INSERT INTO risk_alerts (
        id, tenant_id, customer_id, employee_user_id, detected_by_user_id,
        kind, status, resolved_at, resolved_by_user_id, resolution_note,
        version, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT (
        tenant_id,
        kind,
        COALESCE(customer_id, ''),
        COALESCE(employee_user_id, '')
      ) DO UPDATE SET
        detected_by_user_id = excluded.detected_by_user_id,
        status = excluded.status,
        resolved_at = excluded.resolved_at,
        resolved_by_user_id = excluded.resolved_by_user_id,
        resolution_note = excluded.resolution_note,
        version = risk_alerts.version + 1
      RETURNING *`,
    ).bind(
      record.id,
      record.tenantId,
      record.customerId ?? null,
      record.employeeUserId ?? null,
      record.detectedByUserId,
      record.kind,
      record.status,
      record.resolvedAt ?? null,
      record.resolvedByUserId ?? null,
      record.resolutionNote ?? null,
      record.version,
      record.createdAt,
    ).first<D1Row>();
    if (!row) throw new Error("risk alert upsert failed");
    return mapAlert(row);
  }

  async saveReplyFeedback(input: CreateReplyFeedbackInput): Promise<ReplyFeedbackSaveResult> {
    const parsed = CreateReplyFeedbackInputSchema.parse(input);
    const timestamp = this.now();
    const candidateId = this.createId();
    const reasonCodesJson = JSON.stringify(parsed.reasonCodes);
    const evidenceIdsJson = JSON.stringify(parsed.evidenceIds);
    const privacyReasonCodesJson = JSON.stringify(parsed.privacyReasonCodes);
    const sanitizedFinalReply = parsed.privacyStatus === "safe"
      ? parsed.sanitizedFinalReply ?? null
      : null;
    const insertStatement = this.d1.prepare(
      `INSERT INTO reply_feedback (
        id, tenant_id, employee_user_id, customer_id, decision_card_id,
        card_instance_id, card_digest, agent_version, rating, reason_codes_json,
        scenario, sales_stage, evidence_ids_json, privacy_status,
        privacy_reason_codes_json, sanitized_final_reply, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT (tenant_id, employee_user_id, card_instance_id, agent_version)
      DO NOTHING
      RETURNING *`,
    ).bind(
      candidateId,
      parsed.tenantId,
      parsed.employeeUserId,
      parsed.customerId,
      parsed.decisionCardId,
      parsed.cardInstanceId,
      parsed.cardDigest,
      parsed.agentVersion,
      parsed.rating,
      reasonCodesJson,
      parsed.scenario,
      parsed.salesStage,
      evidenceIdsJson,
      parsed.privacyStatus,
      privacyReasonCodesJson,
      sanitizedFinalReply,
      timestamp,
      timestamp,
    );
    const updateStatement = this.d1.prepare(
      `UPDATE reply_feedback SET
        rating = ?,
        reason_codes_json = ?,
        privacy_status = ?,
        privacy_reason_codes_json = ?,
        sanitized_final_reply = ?,
        updated_at = CASE
          WHEN ? > updated_at THEN ?
          ELSE strftime('%Y-%m-%dT%H:%M:%fZ', updated_at, '+0.001 seconds')
        END
      WHERE tenant_id = ?
        AND employee_user_id = ?
        AND card_instance_id = ?
        AND agent_version = ?
        AND id <> ?
        AND customer_id = ?
        AND decision_card_id = ?
        AND card_digest = ?
        AND scenario = ?
        AND sales_stage = ?
        AND evidence_ids_json = ?
        AND NOT (privacy_status = 'quarantined' AND ? = 'safe')
        AND (
          rating IS NOT ?
          OR reason_codes_json IS NOT ?
          OR privacy_status IS NOT ?
          OR privacy_reason_codes_json IS NOT ?
          OR sanitized_final_reply IS NOT ?
        )
      RETURNING *`,
    ).bind(
      parsed.rating,
      reasonCodesJson,
      parsed.privacyStatus,
      privacyReasonCodesJson,
      sanitizedFinalReply,
      timestamp,
      timestamp,
      parsed.tenantId,
      parsed.employeeUserId,
      parsed.cardInstanceId,
      parsed.agentVersion,
      candidateId,
      parsed.customerId,
      parsed.decisionCardId,
      parsed.cardDigest,
      parsed.scenario,
      parsed.salesStage,
      evidenceIdsJson,
      parsed.privacyStatus,
      parsed.rating,
      reasonCodesJson,
      parsed.privacyStatus,
      privacyReasonCodesJson,
      sanitizedFinalReply,
    );
    const selectStatement = this.d1.prepare(
      `SELECT * FROM reply_feedback
       WHERE tenant_id = ? AND employee_user_id = ?
         AND card_instance_id = ? AND agent_version = ?
       LIMIT 1`,
    ).bind(
      parsed.tenantId,
      parsed.employeeUserId,
      parsed.cardInstanceId,
      parsed.agentVersion,
    );

    if (!this.d1.batch) throw new Error("D1 atomic batch binding unavailable");
    const results = await this.d1.batch([insertStatement, updateStatement, selectStatement]);
    if (results.length !== 3 || results.some((result) => !result.success)) {
      throw new Error("D1 reply feedback atomic batch failed");
    }

    const insertedRow = batchResultRow(results[0]);
    const updatedRow = batchResultRow(results[1]);
    const finalRow = batchResultRow(results[2]);
    if (!finalRow) throw new Error("reply feedback atomic batch returned no record");
    const finalRecord = mapReplyFeedback(finalRow);

    if (insertedRow) {
      mapReplyFeedback(insertedRow);
      return { outcome: "created", record: finalRecord };
    }
    if (updatedRow) {
      mapReplyFeedback(updatedRow);
      return { outcome: "updated", record: finalRecord };
    }
    if (!sameReplyFeedbackSnapshot(finalRecord, parsed)) {
      throw new Error("reply feedback immutable snapshot mismatch");
    }
    if (finalRecord.privacyStatus === "quarantined" && parsed.privacyStatus === "safe") {
      throw new Error("quarantined reply feedback requires guarded replacement");
    }
    if (!sameReplyFeedbackEvaluation(finalRecord, parsed)) {
      throw new Error("reply feedback atomic batch outcome mismatch");
    }
    return { outcome: "unchanged", record: finalRecord };
  }

  async replaceQuarantinedReplyFeedback(
    input: CreateReplyFeedbackInput,
  ): Promise<ReplyFeedbackSaveResult | undefined> {
    const parsed = CreateReplyFeedbackInputSchema.parse(input);
    if (parsed.privacyStatus !== "safe") return undefined;
    const timestamp = this.now();
    const row = await this.d1.prepare(
      `UPDATE reply_feedback SET
        rating = ?,
        reason_codes_json = ?,
        privacy_status = ?,
        privacy_reason_codes_json = ?,
        sanitized_final_reply = ?,
        updated_at = CASE
          WHEN ? > updated_at THEN ?
          ELSE strftime('%Y-%m-%dT%H:%M:%fZ', updated_at, '+0.001 seconds')
        END
      WHERE tenant_id = ?
        AND employee_user_id = ?
        AND card_instance_id = ?
        AND agent_version = ?
        AND privacy_status = 'quarantined'
        AND customer_id = ?
        AND decision_card_id = ?
        AND card_digest = ?
        AND scenario = ?
        AND sales_stage = ?
        AND evidence_ids_json = ?
      RETURNING *`,
    ).bind(
      parsed.rating,
      JSON.stringify(parsed.reasonCodes),
      parsed.privacyStatus,
      JSON.stringify(parsed.privacyReasonCodes),
      parsed.sanitizedFinalReply ?? null,
      timestamp,
      timestamp,
      parsed.tenantId,
      parsed.employeeUserId,
      parsed.cardInstanceId,
      parsed.agentVersion,
      parsed.customerId,
      parsed.decisionCardId,
      parsed.cardDigest,
      parsed.scenario,
      parsed.salesStage,
      JSON.stringify(parsed.evidenceIds),
    ).first<D1Row>();

    return row ? { outcome: "updated", record: mapReplyFeedback(row) } : undefined;
  }

  async listReplyFeedbackForOwner(
    input: OwnerReplyQualityQuery,
  ): Promise<ReplyFeedbackRecord[]> {
    const predicates = ["tenant_id = ?"];
    const bindings: unknown[] = [input.tenantId];
    if (input.employeeUserId) {
      predicates.push("employee_user_id = ?");
      bindings.push(input.employeeUserId);
    }
    if (input.scenario) {
      predicates.push("scenario = ?");
      bindings.push(input.scenario);
    }
    if (input.salesStage) {
      predicates.push("sales_stage = ?");
      bindings.push(input.salesStage);
    }
    if (input.agentVersion) {
      predicates.push("agent_version = ?");
      bindings.push(input.agentVersion);
    }

    const { results } = await this.d1.prepare(
      `SELECT * FROM reply_feedback
       WHERE ${predicates.join(" AND ")}
       ORDER BY updated_at DESC, id ASC`,
    ).bind(...bindings).all<D1Row>();
    return results.map((row) => mapReplyFeedback(row));
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
    const where = ["f.tenant_id = ?"];
    const bindings: unknown[] = [input.tenantId];
    const addFilter = (clause: string, value: unknown): void => {
      where.push(clause);
      bindings.push(value);
    };
    if (filters.from) addFilter("f.created_at >= ?", filters.from);
    if (filters.to) addFilter("f.created_at <= ?", filters.to);
    if (filters.employeeUserId) addFilter("f.employee_user_id = ?", filters.employeeUserId);
    if (filters.customerNumber) addFilter("c.customer_number = ?", filters.customerNumber);
    if (filters.category) addFilter("c.category = ?", filters.category);
    if (filters.result) addFilter("f.result = ?", filters.result);

    const followUpRows = await this.d1.prepare(
      `SELECT f.*, c.customer_number, c.customer_alias, c.category,
              c.created_at AS response_started_at,
              c.customer_fingerprint, f.employee_user_id AS employee_label,
              du.safety_status, du.safety_rule_ids_json
       FROM follow_ups f
       INNER JOIN customers c
         ON c.tenant_id = f.tenant_id AND c.id = f.customer_id
       LEFT JOIN decision_usages du
         ON du.tenant_id = f.tenant_id AND du.follow_up_id = f.id
       WHERE ${where.join(" AND ")}`,
    ).bind(...bindings).all<D1Row>();
    const duplicateCustomerRows = await this.d1.prepare(
      `SELECT id, tenant_id, employee_user_id, customer_fingerprint
       FROM customers
       WHERE tenant_id = ? AND customer_fingerprint IS NOT NULL`,
    ).bind(input.tenantId).all<D1Row>();
    const alertRows = await this.d1.prepare(
      `SELECT * FROM risk_alerts
       WHERE tenant_id = ? AND status = ? ORDER BY created_at DESC`,
    ).bind(input.tenantId, "open").all<D1Row>();

    const records = mergeOwnerDashboardRecords(followUpRows.results).filter((record) => (
      !filters.customerQuery || ownerCustomerMatches(record.customerAlias, record.customerNumber, filters.customerQuery)
    ));
    const duplicateCustomerSources = duplicateCustomerRows.results.map(mapOwnerCustomerRiskRecord);
    const alerts = alertRows.results.map(mapAlert);
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

  private prepareCustomerInsert(record: CustomerRecord): D1PreparedStatementLike {
    return this.d1.prepare(
      `INSERT INTO customers (
        id, tenant_id, employee_user_id, idempotency_key, request_fingerprint, customer_fingerprint,
        customer_number, customer_alias, category, planned_follow_up_at, status,
        version, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      record.id, record.tenantId, record.employeeUserId,
      record.idempotencyKey ?? null, record.requestFingerprint ?? null,
      record.customerFingerprint ?? null,
      record.customerNumber, record.customerAlias, record.category,
      record.plannedFollowUpAt, record.status, record.version, record.createdAt,
    );
  }

  private prepareFollowUpInsert(record: FollowUpRecord): D1PreparedStatementLike {
    return this.d1.prepare(
      `INSERT INTO follow_ups (
        id, tenant_id, customer_id, employee_user_id,
        idempotency_key, request_fingerprint,
        customer_message_vault_asset_id, customer_message_summary,
        planned_follow_up_at, actual_follow_up_at, result, next_action,
        decision_card_id, card_instance_id, reply_usage, chosen_reply_id, final_edited_text,
        version, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      record.id, record.tenantId, record.customerId, record.employeeUserId,
      record.idempotencyKey ?? null, record.requestFingerprint ?? null,
      record.customerMessageVaultAssetId, record.customerMessageSummary,
      record.plannedFollowUpAt,
      record.actualFollowUpAt ?? null, record.result, record.nextAction,
      record.decisionCardId, record.cardInstanceId ?? null,
      record.replyUsage, record.chosenReplyId ?? null,
      record.finalEditedText ?? null, record.version, record.createdAt,
    );
  }

  private prepareDecisionUsageInsert(record: DecisionUsageRecord): D1PreparedStatementLike {
    return this.d1.prepare(
      `INSERT INTO decision_usages (
        id, tenant_id, customer_id, follow_up_id, employee_user_id,
        decision_card_id, card_instance_id, card_digest,
        scenario, delivery_mode, provided_reply_ids_json,
        reply_usage, chosen_reply_id, final_edited_text, copied_at,
        safety_status, safety_rule_ids_json, version, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      record.id, record.tenantId, record.customerId, record.followUpId,
      record.employeeUserId, record.decisionCardId, record.cardInstanceId ?? null,
      record.cardDigest ?? null, record.scenario,
      record.deliveryMode, JSON.stringify(record.providedReplyIds), record.replyUsage,
      record.chosenReplyId ?? null, record.finalEditedText ?? null,
      record.copiedAt ?? null, record.safetyStatus ?? null,
      record.safetyRuleIds ? JSON.stringify(record.safetyRuleIds) : null,
      record.version, record.createdAt,
    );
  }

  private prepareCorrectionInsert(record: CorrectionRecord): D1PreparedStatementLike {
    return this.d1.prepare(
      `INSERT INTO corrections (
        id, tenant_id, customer_id, follow_up_id, actor_user_id,
        idempotency_key, request_fingerprint, reason, version, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      record.id, record.tenantId, record.customerId, record.followUpId,
      record.actorUserId, record.idempotencyKey ?? null,
      record.requestFingerprint ?? null, record.reason, record.version, record.createdAt,
    );
  }

  private prepareVaultAssetInsert(record: VaultAssetRecord): D1PreparedStatementLike {
    return this.d1.prepare(
      `INSERT INTO vault_assets (
        id, tenant_id, customer_id, uploaded_by_user_id, kind, vault_key,
        byte_size, uploaded_at, delete_after, deleted_at, deleted_by_user_id, version, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      record.id, record.tenantId, record.customerId, record.uploadedByUserId,
      record.kind, record.vaultKey, record.byteSize ?? null, record.uploadedAt, record.deleteAfter,
      null, null, record.version, record.createdAt,
    );
  }

  private prepareVaultPendingUploadInsert(
    record: VaultPendingUploadRecord,
  ): D1PreparedStatementLike {
    return this.d1.prepare(
      `INSERT INTO vault_pending_uploads (
        id, tenant_id, customer_id, uploaded_by_user_id, vault_key, byte_size,
        uploaded_at, delete_after, upload_day, version, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      record.id,
      record.tenantId,
      record.customerId,
      record.uploadedByUserId,
      record.vaultKey,
      record.byteSize,
      record.uploadedAt,
      record.deleteAfter,
      record.uploadDay,
      record.version,
      record.createdAt,
    );
  }

  private prepareAuditInsert(record: AuditEvent): D1PreparedStatementLike {
    return this.d1.prepare(
      `INSERT INTO audit_events (
        id, tenant_id, actor_user_id, action, target_kind, target_id,
        occurred_at, details_json, version, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      record.id, record.tenantId, record.actorUserId, record.action,
      record.targetKind, record.targetId, record.occurredAt,
      record.details ? JSON.stringify(record.details) : null,
      record.version, record.createdAt,
    );
  }

  private async runStatement(statement: D1PreparedStatementLike): Promise<void> {
    const result = await statement.run();
    if (!result.success) throw new Error("D1 write failed");
  }

  private async runBatch(statements: readonly D1PreparedStatementLike[]): Promise<void> {
    if (!this.d1.batch) throw new Error("D1 atomic batch binding unavailable");
    const results = await this.d1.batch([...statements]);
    if (results.length !== statements.length || results.some((result) => !result.success)) {
      throw new Error("D1 atomic batch failed");
    }
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

}
