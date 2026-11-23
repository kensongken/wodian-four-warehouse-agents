import { z } from "zod";

import {
  JEWELRY_CATEGORIES,
  type JewelryCategory,
} from "@/domain/decision-card/local-evidence";
import { SALES_SCENARIOS, SALES_STAGES } from "@/domain/decision-card/types";
import { REPLY_FEEDBACK_REASONS } from "@/domain/reply-feedback/schema";
import type {
  FollowUpResult,
  OwnerDashboard,
  OwnerDashboardMetricKey,
  OwnerReplyQualityQuery,
  OwnerReplyQualityView,
  ReplyUsage,
  RiskAlertKind,
  VaultAssetKind,
} from "@/domain/supervision/contracts";
import type { EmployeeTimelineEntryView } from "@/domain/supervision/employee-view";
import { supervisionOperationalDate } from "@/domain/supervision/operational-time";
import type { D1DatabaseLike } from "@/server/supervision/d1-repository";
import { toEmployeeTimelineView } from "@/server/supervision/employee-view";
import type { SupervisionRepository, TimelineEntry } from "@/server/supervision/repository";

const EmployeeRowSchema = z.object({
  id: z.string().trim().min(1),
  email: z.string().trim().email(),
});

const CustomerRowSchema = z.object({
  id: z.string().trim().min(1),
  customer_number: z.string().trim().min(1),
  customer_alias: z.string().trim().min(1),
  employee_user_id: z.string().trim().min(1),
  category: z.enum(JEWELRY_CATEGORIES),
  planned_follow_up_at: z.string().datetime({ offset: true }),
  status: z.enum(["pending", "interested", "won", "lost"]),
});

const VaultAssetRowSchema = z.object({
  id: z.string().trim().min(1),
  kind: z.enum(["customer_identity", "chat_text", "product_image"]),
  delete_after: z.string().datetime({ offset: true }),
});

export type OwnerEmployeeView = Readonly<{
  userId: string;
  navigationId: string;
  displayLabel: string;
}>;

export type OwnerCustomerView = Readonly<{
  id: string;
  navigationId: string;
  customerNumber: string;
  customerAlias: string;
  employeeUserId: string;
  category: JewelryCategory;
  plannedFollowUpAt: string;
  status: FollowUpResult;
}>;

export type OwnerDirectoryView = Readonly<{
  employees: readonly OwnerEmployeeView[];
  customers: readonly OwnerCustomerView[];
}>;

export const OwnerReplyQualityFilterSchema = z.object({
  employeeRef: z.string().trim().min(1).max(120).optional(),
  scenario: z.enum(SALES_SCENARIOS).optional(),
  salesStage: z.enum(SALES_STAGES).optional(),
  agentVersion: z.string().trim().min(1).max(120).optional(),
}).strict();

export type OwnerReplyQualityFilter = z.infer<typeof OwnerReplyQualityFilterSchema>;

const OWNER_REPLY_QUALITY_FILTER_KEYS = new Set([
  "employeeRef",
  "scenario",
  "salesStage",
  "agentVersion",
]);

/** Normalizes native GET-form empties without weakening strict unknown-key validation. */
export function parseOwnerReplyQualityFilters(
  entries: Iterable<readonly [string, unknown]>,
): OwnerReplyQualityFilter {
  const normalizedEntries: Array<readonly [string, unknown]> = [];
  for (const [key, value] of entries) {
    if (OWNER_REPLY_QUALITY_FILTER_KEYS.has(key) && value === "") continue;
    normalizedEntries.push([key, value]);
  }
  return OwnerReplyQualityFilterSchema.parse(Object.fromEntries(normalizedEntries));
}

export type OwnerVaultAssetView = Readonly<{
  id: string;
  kind: VaultAssetKind;
  deleteAfter: string;
}>;

function bytesToHex(bytes: Uint8Array): string { return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join(""); }
async function navigationId(secret: string, tenantId: string, type: "employee" | "customer", id: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${tenantId}\0${type}\0${id}`));
  return `${type}-view-${bytesToHex(new Uint8Array(signature)).slice(0, 32)}`;
}
function safeEqual(a: string, b: string): boolean { const x = new TextEncoder().encode(a), y = new TextEncoder().encode(b); let diff = x.length ^ y.length; const size = Math.max(x.length, y.length); for (let i=0;i<size;i+=1) diff |= (x[i%x.length]??0) ^ (y[i%y.length]??0); return diff === 0; }
export function resolveOwnerEmployeeRef(directory: OwnerDirectoryView, ref: string): OwnerEmployeeView | undefined { let match: OwnerEmployeeView|undefined; for (const item of directory.employees) if (safeEqual(item.navigationId, ref)) match=item; return match; }
export function resolveOwnerCustomerRef(directory: OwnerDirectoryView, ref: string): OwnerCustomerView | undefined { let match: OwnerCustomerView|undefined; for (const item of directory.customers) if (safeEqual(item.navigationId, ref)) match=item; return match; }

function toEmployeeView(input: unknown, navigationIdValue: string): OwnerEmployeeView {
  const row = EmployeeRowSchema.parse(input);
  return {
    userId: row.id,
    navigationId: navigationIdValue,
    displayLabel: row.email.toLowerCase(),
  };
}

function toCustomerView(input: unknown, navigationIdValue: string): OwnerCustomerView {
  const row = CustomerRowSchema.parse(input);
  return {
    id: row.id,
    navigationId: navigationIdValue,
    customerNumber: row.customer_number,
    customerAlias: row.customer_alias,
    employeeUserId: row.employee_user_id,
    category: row.category,
    plannedFollowUpAt: row.planned_follow_up_at,
    status: row.status,
  };
}

export type OwnerBrowserMetric = Readonly<{ key: string; label: string; valueLabel: string; recordRefs: readonly string[] }>;
export type OwnerBrowserRecord = Readonly<{
  ref: string; customerNavigationId?: string; customerNumber: string; customerAlias: string;
  employeeNavigationId?: string; employeeLabel: string; category: JewelryCategory;
  submittedAt: string; plannedFollowUpAt: string; actualFollowUpAt?: string;
  decisionUsage: ReplyUsage; nextAction: string; result: FollowUpResult; riskLabels: readonly string[];
}>;
export type OwnerBrowserEmployee = Readonly<{ navigationId: string; displayLabel: string; metrics: readonly OwnerBrowserMetric[] }>;
export type OwnerBrowserCustomer = Readonly<{ navigationId: string; customerNumber: string; customerAlias: string; category: JewelryCategory; status: FollowUpResult }>;
export type OwnerBrowserRisk = Readonly<{ kind: RiskAlertKind; label: string; recordRefs: readonly string[] }>;
export type OwnerDashboardBrowserView = Readonly<{
  generatedAt: string; summaryMetrics: readonly OwnerBrowserMetric[]; employees: readonly OwnerBrowserEmployee[];
  records: readonly OwnerBrowserRecord[]; customers: readonly OwnerBrowserCustomer[]; risks: readonly OwnerBrowserRisk[];
}>;

const RISK_LABELS: Record<RiskAlertKind, string> = {
  overdue_follow_up: "跟进超期", missing_result: "结果缺失", duplicate_customer: "疑似重复客户",
  sales_safety: "销售安全待复核", retention_failed: "原始资料清理待复核",
};

export function toOwnerDashboardBrowserView(data: OwnerDashboard, directory: OwnerDirectoryView): OwnerDashboardBrowserView {
  const refs = new Map(data.records.map((record, index) => [record.id, `row-${index + 1}`]));
  const customerRefs = new Map(directory.customers.map((c) => [c.id, `customer-row-${c.navigationId}`]));
  const safeRefs = (ids: readonly string[]) => ids.flatMap((id) => refs.get(id) ? [refs.get(id)!] : customerRefs.get(id) ? [customerRefs.get(id)!] : []);
  const employeeById = new Map(directory.employees.map((item) => [item.userId, item]));
  const customerByNumber = new Map(directory.customers.map((item) => [item.customerNumber, item]));
  const metric = (key: OwnerDashboardMetricKey, label: string, valueLabel: string, ids: readonly string[]): OwnerBrowserMetric =>
    ({ key, label, valueLabel, recordRefs: safeRefs(ids) });
  const generatedDay = supervisionOperationalDate(data.generatedAt);
  const todayIds = data.records
    .filter((record) => supervisionOperationalDate(record.submittedAt) === generatedDay)
    .map((record) => record.id);
  const employees = directory.employees.map((dir) => {
    const source = data.employees.find((item) => item.employeeUserId === dir.userId);
    const ids = (key: OwnerDashboardMetricKey) => source?.drillDownRecordIds[key] ?? [];
    const count = (key: Exclude<OwnerDashboardMetricKey, "averageResponseDurationMs" | "completionRate">) => source?.[key] ?? 0;
    const due = source?.due ?? 0; const completed = ids("completionRate").length; const percent = source?.completionRate ?? 0;
    return { navigationId: dir.navigationId, displayLabel: dir.displayLabel, metrics: [
      metric("submitted", "提交", String(count("submitted")), ids("submitted")),
      metric("completionRate", "完成", `${completed}/${due}（${percent}%）`, ids("completionRate")),
      metric("overdue", "超期", String(count("overdue")), ids("overdue")),
      metric("adopted", "采用", String(count("adopted")), ids("adopted")), metric("edited", "改写", String(count("edited")), ids("edited")),
      metric("notUsed", "未用", String(count("notUsed")), ids("notUsed")), metric("won", "成交", String(count("won")), ids("won")),
      metric("interested", "意向", String(count("interested")), ids("interested")), metric("lost", "未成交", String(count("lost")), ids("lost")),
    ] };
  });
  return {
    generatedAt: data.generatedAt,
    summaryMetrics: [
      { key: "todaySubmitted", label: "今日提交", valueLabel: String(todayIds.length), recordRefs: safeRefs(todayIds) },
      metric("pending", "待跟进", String(data.totals.pending), data.totals.drillDownRecordIds.pending),
      metric("overdue", "已超期", String(data.totals.overdue), data.totals.drillDownRecordIds.overdue),
      metric("won", "已成交", String(data.totals.won), data.totals.drillDownRecordIds.won),
    ], employees,
    records: data.records.map((r) => ({ ref: refs.get(r.id)!, customerNavigationId: customerByNumber.get(r.customerNumber)?.navigationId,
      customerNumber: r.customerNumber, customerAlias: r.customerAlias, employeeNavigationId: employeeById.get(r.employeeUserId)?.navigationId,
      employeeLabel: employeeById.get(r.employeeUserId)?.displayLabel ?? "已停用员工", category: r.category, submittedAt: r.submittedAt,
      plannedFollowUpAt: r.plannedFollowUpAt, actualFollowUpAt: r.actualFollowUpAt, decisionUsage: r.decisionUsage,
      nextAction: r.nextAction, result: r.result, riskLabels: r.riskLabels })),
    customers: directory.customers.map((c) => ({ navigationId: c.navigationId, customerNumber: c.customerNumber, customerAlias: c.customerAlias, category: c.category, status: c.status })),
    risks: data.riskSignals.map((r) => ({ kind: r.kind, label: RISK_LABELS[r.kind], recordRefs: safeRefs(r.recordIds) })).filter((r) => r.recordRefs.length > 0),
  };
}

function toVaultAssetView(input: unknown): OwnerVaultAssetView {
  const row = VaultAssetRowSchema.parse(input);
  return {
    id: row.id,
    kind: row.kind,
    deleteAfter: row.delete_after,
  };
}

/** Loads only owner-page display fields. Account labels never alter dashboard/export DTOs. */
export async function getOwnerDirectory(
  d1: D1DatabaseLike,
  tenantId: string,
  navigationSecret: string,
): Promise<OwnerDirectoryView> {
  const [employeeRows, customerRows] = await Promise.all([
    d1.prepare(
      `SELECT id, email
       FROM users
       WHERE tenant_id = ? AND role = 'employee' AND active = 1
       ORDER BY LOWER(email) ASC`,
    ).bind(tenantId).all<Record<string, unknown>>(),
    d1.prepare(
      `SELECT id, customer_number, customer_alias, employee_user_id,
              category, planned_follow_up_at, status
       FROM customers
       WHERE tenant_id = ?
       ORDER BY created_at DESC`,
    ).bind(tenantId).all<Record<string, unknown>>(),
  ]);

  return {
    employees: await Promise.all(employeeRows.results.map(async (row) => toEmployeeView(row, await navigationId(navigationSecret, tenantId, "employee", EmployeeRowSchema.parse(row).id)))),
    customers: await Promise.all(customerRows.results.map(async (row) => toCustomerView(row, await navigationId(navigationSecret, tenantId, "customer", CustomerRowSchema.parse(row).id)))),
  };
}

/**
 * Converts tenant-scoped feedback into the owner allowlist. Internal row,
 * employee, customer, card, evidence, and tenant identifiers have no path
 * into the returned shape.
 */
export async function getOwnerReplyQualityView(
  repository: SupervisionRepository,
  directory: OwnerDirectoryView,
  query: OwnerReplyQualityQuery,
): Promise<OwnerReplyQualityView> {
  const records = await repository.listReplyFeedbackForOwner(query);
  const employees = new Map(directory.employees.map((employee) => [employee.userId, employee]));
  const customers = new Map(directory.customers.map((customer) => [customer.id, customer]));
  const reasonCounts = new Map(REPLY_FEEDBACK_REASONS.map((code) => [code, 0]));

  for (const record of records) {
    for (const code of record.reasonCodes) {
      reasonCounts.set(code, (reasonCounts.get(code) ?? 0) + 1);
    }
  }

  return {
    totals: {
      helpful: records.filter((record) => record.rating === "helpful").length,
      needsImprovement: records.filter((record) => record.rating === "needs_improvement").length,
      quarantined: records.filter((record) => record.privacyStatus === "quarantined").length,
    },
    reasons: REPLY_FEEDBACK_REASONS.flatMap((code) => {
      const count = reasonCounts.get(code) ?? 0;
      return count > 0 ? [{ code, count }] : [];
    }),
    samples: records.map((record, index) => {
      const common = {
        feedbackRef: `feedback-row-${index + 1}`,
        employeeRef: employees.get(record.employeeUserId)?.navigationId ?? "employee-unavailable",
        customerRef: customers.get(record.customerId)?.navigationId ?? "customer-unavailable",
        scenario: record.scenario,
        salesStage: record.salesStage,
        agentVersion: record.agentVersion,
        rating: record.rating,
        privacyReasonCodes: [...record.privacyReasonCodes],
      };
      if (record.privacyStatus === "quarantined") {
        return { ...common, privacyStatus: "quarantined" as const };
      }
      return {
        ...common,
        privacyStatus: "safe" as const,
        ...(record.sanitizedFinalReply === undefined
          ? {}
          : { sanitizedFinalReply: record.sanitizedFinalReply }),
      };
    }),
  };
}

export async function getOwnerCustomerVaultAssets(
  d1: D1DatabaseLike,
  tenantId: string,
  customerId: string,
  now: string,
): Promise<OwnerVaultAssetView[]> {
  const { results } = await d1.prepare(
    `SELECT id, kind, delete_after
     FROM vault_assets
     WHERE tenant_id = ? AND customer_id = ?
       AND deleted_at IS NULL AND delete_after > ?
     ORDER BY uploaded_at DESC`,
  ).bind(tenantId, customerId, now).all<Record<string, unknown>>();
  return results.map(toVaultAssetView);
}

export async function getOwnerVaultAssetView(
  d1: D1DatabaseLike,
  tenantId: string,
  assetId: string,
  now: string,
): Promise<OwnerVaultAssetView | undefined> {
  const row = await d1.prepare(
    `SELECT id, kind, delete_after
     FROM vault_assets
     WHERE tenant_id = ? AND id = ?
       AND deleted_at IS NULL AND delete_after > ?
     LIMIT 1`,
  ).bind(tenantId, assetId, now).first<Record<string, unknown>>();
  return row ? toVaultAssetView(row) : undefined;
}

/** Reuses the established immutable, de-identified timeline allowlist. */
export function toOwnerTimelineView(
  timeline: readonly TimelineEntry[],
  employeeLabels: ReadonlyMap<string, string>,
): EmployeeTimelineEntryView[] {
  return toEmployeeTimelineView(
    timeline,
    (actorUserId) => employeeLabels.get(actorUserId) ?? "店员",
  );
}
