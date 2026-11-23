import { describe, expect, it } from "vitest";

import {
  getOwnerCustomerVaultAssets,
  getOwnerDirectory,
  getOwnerVaultAssetView,
  resolveOwnerCustomerRef,
  resolveOwnerEmployeeRef,
  toOwnerDashboardBrowserView,
  toOwnerTimelineView,
} from "@/server/supervision/owner-view";
import type { OwnerDashboard } from "@/domain/supervision/contracts";
import type { D1DatabaseLike, D1PreparedStatementLike } from "@/server/supervision/d1-repository";
import type { TimelineEntry } from "@/server/supervision/repository";

class QueryD1 implements D1DatabaseLike {
  readonly calls: Array<{ sql: string; bindings: unknown[] }> = [];

  prepare(sql: string): D1PreparedStatementLike {
    const call = { sql, bindings: [] as unknown[] };
    this.calls.push(call);
    const rows = sql.includes("FROM users")
      ? [{ id: "employee-1", email: "employee@shop.test" }]
      : sql.includes("FROM customers")
        ? [{
            id: "customer-1",
            customer_number: "C-0001",
            customer_alias: "林女士",
            employee_user_id: "employee-1",
            category: "沉香",
            planned_follow_up_at: "2026-08-31T08:00:00.000Z",
            status: "pending",
            customer_fingerprint: "SECRET_FINGERPRINT",
          }]
        : [{
            id: "asset-1",
            customer_id: "customer-1",
            kind: "chat_text",
            uploaded_at: "2026-08-30T08:00:00.000Z",
            delete_after: "2026-09-29T08:00:00.000Z",
            vault_key: "SECRET_VAULT_KEY",
          }];
    const statement: D1PreparedStatementLike = {
      bind: (...bindings) => {
        call.bindings = bindings;
        return statement;
      },
      first: async <T>() => (rows[0] ?? null) as T | null,
      all: async <T>() => ({ results: rows as T[] }),
      run: async () => ({ success: true }),
    };
    return statement;
  }
}

describe("owner safe view boundaries", () => {
  it("serializes an explicit browser DTO without tenant, alerts, source IDs, or account IDs", () => {
    const dashboard = {
      tenantId: "SECRET_TENANT_ID",
      generatedAt: "2026-08-30T12:00:00.000Z",
      totals: {
        submitted: 1, followedUp: 1, pending: 0, due: 1, overdue: 0,
        interested: 0, won: 1, lost: 0, adopted: 1, edited: 0, notUsed: 0,
        missingResult: 0, averageResponseDurationMs: 1_000, completionRate: 100,
        drillDownRecordIds: {
          submitted: ["SECRET_FOLLOW_UP_ID"], followedUp: ["SECRET_FOLLOW_UP_ID"],
          pending: [], due: ["SECRET_FOLLOW_UP_ID"], overdue: [], interested: [],
          won: ["SECRET_FOLLOW_UP_ID"], lost: [], adopted: ["SECRET_FOLLOW_UP_ID"],
          edited: [], notUsed: [], missingResult: [],
          averageResponseDurationMs: ["SECRET_FOLLOW_UP_ID"], completionRate: ["SECRET_FOLLOW_UP_ID"],
        },
      },
      employees: [{
        employeeUserId: "SECRET_EMPLOYEE_ACCOUNT_ID",
        employeeLabel: "SECRET_EMPLOYEE_ACCOUNT_ID",
        submitted: 1, followedUp: 1, pending: 0, due: 1, overdue: 0,
        interested: 0, won: 1, lost: 0, adopted: 1, edited: 0, notUsed: 0,
        missingResult: 0, averageResponseDurationMs: 1_000, completionRate: 100,
        drillDownRecordIds: {
          submitted: ["SECRET_FOLLOW_UP_ID"], followedUp: ["SECRET_FOLLOW_UP_ID"],
          pending: [], due: ["SECRET_FOLLOW_UP_ID"], overdue: [], interested: [],
          won: ["SECRET_FOLLOW_UP_ID"], lost: [], adopted: ["SECRET_FOLLOW_UP_ID"],
          edited: [], notUsed: [], missingResult: [],
          averageResponseDurationMs: ["SECRET_FOLLOW_UP_ID"], completionRate: ["SECRET_FOLLOW_UP_ID"],
        },
      }],
      records: [{
        id: "SECRET_FOLLOW_UP_ID",
        customerNumber: "C-0001",
        customerAlias: "林女士",
        employeeUserId: "SECRET_EMPLOYEE_ACCOUNT_ID",
        employeeLabel: "SECRET_EMPLOYEE_ACCOUNT_ID",
        category: "沉香",
        submittedAt: "2026-08-30T08:00:00.000Z",
        plannedFollowUpAt: "2026-08-30T09:00:00.000Z",
        actualFollowUpAt: "2026-08-30T09:00:00.000Z",
        decisionUsage: "adopted",
        nextAction: "成交后回访",
        result: "won",
        riskLabels: ["sales_safety"],
      }],
      riskSignals: [{
        id: "SECRET_RISK_SOURCE_ID",
        tenantId: "SECRET_TENANT_ID",
        kind: "sales_safety",
        employeeUserId: "SECRET_EMPLOYEE_ACCOUNT_ID",
        recordIds: ["SECRET_FOLLOW_UP_ID"],
        labels: ["SECRET_AUDIT_LABEL"],
        detectedAt: "2026-08-30T12:00:00.000Z",
        sourceAlertId: "SECRET_ALERT_SOURCE_ID",
      }],
      alerts: [{
        id: "SECRET_ALERT_ID",
        tenantId: "SECRET_TENANT_ID",
        detectedByUserId: "SECRET_OWNER_ACCOUNT_ID",
        kind: "sales_safety",
        status: "open",
        version: 1,
        createdAt: "2026-08-30T12:00:00.000Z",
      }],
    } satisfies OwnerDashboard;
    const directory = {
      employees: [{
        userId: "SECRET_EMPLOYEE_ACCOUNT_ID",
        navigationId: "employee-view-safe",
        displayLabel: "employee@shop.test",
      }],
      customers: [{
        id: "SECRET_CUSTOMER_INTERNAL_ID",
        navigationId: "customer-view-safe",
        customerNumber: "C-0001",
        customerAlias: "林女士",
        employeeUserId: "SECRET_EMPLOYEE_ACCOUNT_ID",
        category: "沉香" as const,
        plannedFollowUpAt: "2026-08-30T09:00:00.000Z",
        status: "won" as const,
      }],
    };
    const view = toOwnerDashboardBrowserView(dashboard, directory);
    const serialized = JSON.stringify(view);
    expect(view.records[0]).toMatchObject({
      ref: "row-1",
      customerNavigationId: "customer-view-safe",
      employeeNavigationId: "employee-view-safe",
    });
    for (const secret of [
      "SECRET_TENANT_ID", "SECRET_FOLLOW_UP_ID", "SECRET_EMPLOYEE_ACCOUNT_ID",
      "SECRET_CUSTOMER_INTERNAL_ID", "SECRET_RISK_SOURCE_ID", "SECRET_ALERT_SOURCE_ID",
      "SECRET_ALERT_ID", "SECRET_AUDIT_LABEL", "SECRET_OWNER_ACCOUNT_ID",
    ]) expect(serialized).not.toContain(secret);
    expect(serialized).not.toMatch(/tenantId|alerts|sourceAlertId|employeeUserId|recordIds/);
  });

  it("counts today using the Asia/Taipei operational date across the UTC boundary", () => {
    const metricIds = (ids: string[]) => ({
      submitted: ids,
      followedUp: [],
      pending: ids,
      due: [],
      overdue: [],
      interested: [],
      won: [],
      lost: [],
      adopted: [],
      edited: [],
      notUsed: ids,
      missingResult: [],
      averageResponseDurationMs: [],
      completionRate: [],
    });
    const record = (id: string, submittedAt: string) => ({
      id,
      customerNumber: `C-${id}`,
      customerAlias: id,
      employeeUserId: "employee-1",
      employeeLabel: "employee-1",
      category: "沉香" as const,
      submittedAt,
      plannedFollowUpAt: "2026-09-01T08:00:00.000Z",
      decisionUsage: "not_used" as const,
      nextAction: "稍后跟进",
      result: "pending" as const,
      riskLabels: [],
    });
    const records = [
      record("taipei-today", "2026-08-30T16:05:00.000Z"),
      record("taipei-yesterday", "2026-08-30T15:59:59.999Z"),
    ];
    const dashboard = {
      tenantId: "tenant-a",
      generatedAt: "2026-08-30T16:30:00.000Z",
      totals: {
        submitted: 2,
        followedUp: 0,
        pending: 2,
        due: 0,
        overdue: 0,
        interested: 0,
        won: 0,
        lost: 0,
        adopted: 0,
        edited: 0,
        notUsed: 2,
        missingResult: 0,
        averageResponseDurationMs: null,
        completionRate: 0,
        drillDownRecordIds: metricIds(records.map((item) => item.id)),
      },
      employees: [],
      records,
      riskSignals: [],
      alerts: [],
    } satisfies OwnerDashboard;

    const view = toOwnerDashboardBrowserView(dashboard, {
      employees: [],
      customers: [],
    });

    expect(view.summaryMetrics[0]).toMatchObject({
      key: "todaySubmitted",
      valueLabel: "1",
      recordRefs: ["row-1"],
    });
  });

  it("discovers active tenant employees and every tenant customer through explicit allowlists", async () => {
    const d1 = new QueryD1();
    const view = await getOwnerDirectory(d1, "tenant-a", "test-navigation-secret-32-bytes-long");

    expect(view).toMatchObject({
      employees: [{ userId: "employee-1", displayLabel: "employee@shop.test" }],
      customers: [{
        id: "customer-1",
        customerNumber: "C-0001",
        customerAlias: "林女士",
        employeeUserId: "employee-1",
        category: "沉香",
        plannedFollowUpAt: "2026-08-31T08:00:00.000Z",
        status: "pending",
      }],
    });
    expect(view.employees[0]?.navigationId).toMatch(/^employee-view-[0-9a-f]{32}$/u);
    expect(view.customers[0]?.navigationId).toMatch(/^customer-view-[0-9a-f]{32}$/u);
    expect(d1.calls[0]?.sql).toContain("active = 1");
    expect(d1.calls[0]?.sql).toContain("role = 'employee'");
    expect(d1.calls[0]?.bindings).toEqual(["tenant-a"]);
    expect(d1.calls[1]?.bindings).toEqual(["tenant-a"]);
    expect(JSON.stringify(view)).not.toMatch(/SECRET|fingerprint|tenantId|vault/i);
  });

  it("keeps signed refs stable while binding them to tenant and entity type", async () => {
    const d1 = new QueryD1();
    const first = await getOwnerDirectory(d1, "tenant-a", "test-navigation-secret-32-bytes-long");
    const again = await getOwnerDirectory(d1, "tenant-a", "test-navigation-secret-32-bytes-long");
    const otherTenant = await getOwnerDirectory(d1, "tenant-b", "test-navigation-secret-32-bytes-long");
    expect(again.employees[0]?.navigationId).toBe(first.employees[0]?.navigationId);
    expect(otherTenant.employees[0]?.navigationId).not.toBe(first.employees[0]?.navigationId);
    expect(first.customers[0]?.navigationId).not.toBe(first.employees[0]?.navigationId.replace("employee-view-", "customer-view-"));
    expect(resolveOwnerCustomerRef(first, first.employees[0]!.navigationId)).toBeUndefined();
    expect(resolveOwnerEmployeeRef(otherTenant, first.employees[0]!.navigationId)).toBeUndefined();
  });

  it("keeps zero-followup duplicate-customer risks visible through safe customer refs", () => {
    const dashboard = { tenantId: "tenant-a", generatedAt: "2026-08-30T12:00:00.000Z", totals: { submitted:0,followedUp:0,pending:0,due:0,overdue:0,interested:0,won:0,lost:0,adopted:0,edited:0,notUsed:0,missingResult:0,averageResponseDurationMs:null,completionRate:0,drillDownRecordIds:{submitted:[],followedUp:[],pending:[],due:[],overdue:[],interested:[],won:[],lost:[],adopted:[],edited:[],notUsed:[],missingResult:[],averageResponseDurationMs:[],completionRate:[]} }, employees:[],records:[],alerts:[],riskSignals:[{id:"risk-secret",tenantId:"tenant-a",kind:"duplicate_customer",recordIds:["customer-secret"],labels:[],detectedAt:"2026-08-30T12:00:00.000Z"}] } satisfies OwnerDashboard;
    const directory = { employees:[], customers:[{id:"customer-secret",navigationId:"customer-view-safe",customerNumber:"C-1",customerAlias:"林女士",employeeUserId:"employee-secret",category:"沉香" as const,plannedFollowUpAt:"2026-08-31T00:00:00.000Z",status:"pending" as const}] };
    expect(toOwnerDashboardBrowserView(dashboard,directory).risks[0]?.recordRefs).toEqual(["customer-row-customer-view-safe"]);
  });

  it("returns only live vault metadata and never returns its storage key", async () => {
    const d1 = new QueryD1();

    await expect(getOwnerCustomerVaultAssets(
      d1,
      "tenant-a",
      "customer-1",
      "2026-08-30T12:00:00.000Z",
    )).resolves.toEqual([{
      id: "asset-1",
      kind: "chat_text",
      deleteAfter: "2026-09-29T08:00:00.000Z",
    }]);
    await expect(getOwnerVaultAssetView(
      d1,
      "tenant-a",
      "asset-1",
      "2026-08-30T12:00:00.000Z",
    )).resolves.toEqual({
      id: "asset-1",
      kind: "chat_text",
      deleteAfter: "2026-09-29T08:00:00.000Z",
    });

    expect(JSON.stringify(await getOwnerVaultAssetView(
      d1,
      "tenant-a",
      "asset-1",
      "2026-08-30T12:00:00.000Z",
    ))).not.toContain("SECRET_VAULT_KEY");
  });

  it("maps the immutable timeline without raw vault references or edited reply text", () => {
    const timeline: TimelineEntry[] = [{
      kind: "follow_up",
      record: {
        id: "follow-up-1",
        tenantId: "tenant-secret",
        customerId: "customer-1",
        employeeUserId: "employee-1",
        customerMessageVaultAssetId: "vault-secret",
        customerMessageSummary: "客户询问保养方式",
        plannedFollowUpAt: "2026-08-31T08:00:00.000Z",
        result: "pending",
        nextAction: "周一回访",
        decisionCardId: "card-secret",
        replyUsage: "edited",
        finalEditedText: "SECRET_FINAL_REPLY",
        version: 1,
        createdAt: "2026-08-30T08:00:00.000Z",
      },
    }];

    const view = toOwnerTimelineView(timeline, new Map([["employee-1", "小吴"]]));

    expect(view[0]).toMatchObject({
      id: "follow-up-1",
      actorLabel: "小吴",
      customerMessageSummary: "客户询问保养方式",
    });
    expect(JSON.stringify(view)).not.toMatch(/tenant-secret|vault-secret|card-secret|SECRET_FINAL_REPLY/);
  });
});
