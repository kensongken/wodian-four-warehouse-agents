import { describe, expect, it } from "vitest";

import type { OwnerDashboardRecord } from "@/domain/supervision/dashboard";
import {
  evaluateRiskAlerts,
  riskLabelsForDashboardRecord,
} from "@/domain/supervision/risk";

const NOW = "2026-08-30T12:00:00.000Z";

function record(
  overrides: Partial<OwnerDashboardRecord> & Pick<OwnerDashboardRecord, "id" | "customerId">,
): OwnerDashboardRecord {
  return {
    tenantId: "tenant-a",
    customerNumber: `C-${overrides.customerId}`,
    customerAlias: `客户-${overrides.customerId}`,
    employeeUserId: "employee-a",
    employeeLabel: "员工 A",
    category: "沉香",
    submittedAt: "2026-08-28T08:00:00.000Z",
    plannedFollowUpAt: "2026-08-31T09:00:00.000Z",
    result: "pending",
    replyUsage: "not_used",
    nextAction: "人工跟进",
    decisionCardId: `card-${overrides.id}`,
    ...overrides,
    id: overrides.id,
    customerId: overrides.customerId,
  };
}

describe("owner risk signals", () => {
  it("creates tenant-scoped operational signals for all five configured rules", () => {
    const signals = evaluateRiskAlerts([
      record({
        id: "follow-up-overdue",
        customerId: "customer-overdue",
        plannedFollowUpAt: "2026-08-30T11:00:00.000Z",
      }),
      record({
        id: "follow-up-missing",
        customerId: "customer-missing",
        actualFollowUpAt: "2026-08-29T11:59:59.000Z",
      }),
      record({
        id: "follow-up-duplicate-a",
        customerId: "customer-duplicate-a",
        employeeUserId: "employee-a",
        customerFingerprint: "fp_same-customer",
      }),
      record({
        id: "follow-up-duplicate-b",
        customerId: "customer-duplicate-b",
        employeeUserId: "employee-b",
        customerFingerprint: "fp_same-customer",
      }),
      record({
        id: "follow-up-safety",
        customerId: "customer-safety",
        safetyFlags: ["PRICE_OR_PAYMENT_HUMAN_REVIEW"],
      }),
      record({
        id: "follow-up-retention",
        customerId: "customer-retention",
        retentionFailed: true,
      }),
      record({
        id: "other-tenant-same-fingerprint",
        tenantId: "tenant-b",
        customerId: "customer-other-tenant",
        employeeUserId: "employee-c",
        customerFingerprint: "fp_same-customer",
      }),
    ], NOW);

    expect(signals.map((signal) => signal.kind)).toEqual(expect.arrayContaining([
      "overdue_follow_up",
      "missing_result",
      "duplicate_customer",
      "sales_safety",
      "retention_failed",
    ]));
    const duplicate = signals.find((signal) => signal.kind === "duplicate_customer");
    expect(duplicate).toMatchObject({
      tenantId: "tenant-a",
      recordIds: ["customer-duplicate-a", "customer-duplicate-b"],
    });
    expect(duplicate?.recordIds).not.toContain("customer-other-tenant");
    expect(signals.find((signal) => signal.kind === "sales_safety")?.labels)
      .toEqual(["PRICE_OR_PAYMENT_HUMAN_REVIEW"]);
    expect(JSON.stringify(signals)).not.toContain("fp_same-customer");
  });

  it("waits the full 24 hours before raising a missing-result signal", () => {
    const signals = evaluateRiskAlerts([
      record({
        id: "follow-up-boundary",
        customerId: "customer-boundary",
        actualFollowUpAt: "2026-08-29T12:00:00.001Z",
      }),
    ], NOW);

    expect(signals.some((signal) => signal.kind === "missing_result")).toBe(false);
  });

  it("returns review signals only and never employee grades or discipline conclusions", () => {
    const signals = evaluateRiskAlerts([
      record({
        id: "follow-up-overdue",
        customerId: "customer-overdue",
        plannedFollowUpAt: "2026-08-30T11:00:00.000Z",
      }),
    ], NOW);

    expect(JSON.stringify(signals)).not.toMatch(/grade|score|disciplin|performance|punish/i);
  });

  it("deduplicates repeated joined rows before evaluating signals", () => {
    const overdue = record({
      id: "follow-up-overdue",
      customerId: "customer-overdue",
      plannedFollowUpAt: "2026-08-30T11:00:00.000Z",
    });

    const signals = evaluateRiskAlerts([overdue, overdue], NOW);

    expect(signals.filter((signal) => signal.kind === "overdue_follow_up")).toHaveLength(1);
  });

  it("evaluates duplicate customer registrations independently from follow-up rows", () => {
    const signals = evaluateRiskAlerts([], NOW, [], [
      {
        id: "customer-duplicate-a",
        tenantId: "tenant-a",
        customerId: "customer-duplicate-a",
        employeeUserId: "employee-a",
        customerFingerprint: "fp_same-customer",
      },
      {
        id: "customer-duplicate-b",
        tenantId: "tenant-a",
        customerId: "customer-duplicate-b",
        employeeUserId: "employee-b",
        customerFingerprint: "fp_same-customer",
      },
    ]);

    expect(signals).toEqual([
      expect.objectContaining({
        kind: "duplicate_customer",
        tenantId: "tenant-a",
        recordIds: ["customer-duplicate-a", "customer-duplicate-b"],
      }),
    ]);
  });

  it("uses unique customer IDs and attaches duplicate labels in the public fallback path", () => {
    const records = [
      record({
        id: "follow-up-customer-a-1",
        customerId: "customer-a",
        customerFingerprint: "fp_same-customer",
      }),
      record({
        id: "follow-up-customer-a-2",
        customerId: "customer-a",
        customerFingerprint: "fp_same-customer",
      }),
      record({
        id: "follow-up-customer-b",
        customerId: "customer-b",
        customerFingerprint: "fp_same-customer",
      }),
    ];

    const signals = evaluateRiskAlerts(records, NOW);
    const duplicate = signals.find((signal) => signal.kind === "duplicate_customer");

    expect(duplicate?.recordIds).toEqual(["customer-a", "customer-b"]);
    expect(duplicate?.recordIds).not.toEqual(expect.arrayContaining([
      "follow-up-customer-a-1",
      "follow-up-customer-a-2",
      "follow-up-customer-b",
    ]));
    expect(records.map((entry) => riskLabelsForDashboardRecord(entry, signals))).toEqual([
      ["duplicate_customer"],
      ["duplicate_customer"],
      ["duplicate_customer"],
    ]);
  });

  it("keeps fallback customer identity tenant-qualified when tenants share a customer ID", () => {
    const records = [
      record({
        id: "follow-up-tenant-a-shared",
        tenantId: "tenant-a",
        customerId: "customer-shared",
        customerFingerprint: "fp_tenant-a-duplicate",
      }),
      record({
        id: "follow-up-tenant-a-peer",
        tenantId: "tenant-a",
        customerId: "customer-tenant-a-peer",
        customerFingerprint: "fp_tenant-a-duplicate",
      }),
      record({
        id: "follow-up-tenant-b-shared",
        tenantId: "tenant-b",
        customerId: "customer-shared",
        employeeUserId: "employee-b",
        customerFingerprint: "fp_tenant-b-only",
      }),
    ];

    const signals = evaluateRiskAlerts(records, NOW);
    const duplicates = signals.filter((signal) => signal.kind === "duplicate_customer");

    expect(duplicates).toEqual([
      expect.objectContaining({
        tenantId: "tenant-a",
        recordIds: ["customer-shared", "customer-tenant-a-peer"],
      }),
    ]);
    expect(records.map((entry) => riskLabelsForDashboardRecord(entry, signals))).toEqual([
      ["duplicate_customer"],
      ["duplicate_customer"],
      [],
    ]);
  });

  it("treats a future actual-follow-up timestamp as absent for overdue rules", () => {
    const signals = evaluateRiskAlerts([
      record({
        id: "follow-up-future-actual",
        customerId: "customer-future-actual",
        plannedFollowUpAt: "2026-08-30T11:00:00.000Z",
        actualFollowUpAt: "2026-08-30T12:00:00.001Z",
      }),
    ], NOW);

    expect(signals.map((signal) => signal.kind)).toContain("overdue_follow_up");
    expect(signals.map((signal) => signal.kind)).not.toContain("missing_result");
  });

  it("emits a generic safety signal when review is required without rule IDs", () => {
    const generic = evaluateRiskAlerts([
      record({
        id: "follow-up-generic-review",
        customerId: "customer-generic-review",
        safetyReviewRequired: true,
        safetyFlags: [],
      }),
    ], NOW);
    const specific = evaluateRiskAlerts([
      record({
        id: "follow-up-specific-review",
        customerId: "customer-specific-review",
        safetyReviewRequired: true,
        safetyFlags: ["PRICE_OR_PAYMENT_HUMAN_REVIEW"],
      }),
    ], NOW);

    expect(generic.find((signal) => signal.kind === "sales_safety")?.labels)
      .toEqual(["review_required"]);
    expect(specific.find((signal) => signal.kind === "sales_safety")?.labels)
      .toEqual(["PRICE_OR_PAYMENT_HUMAN_REVIEW"]);
  });
});
