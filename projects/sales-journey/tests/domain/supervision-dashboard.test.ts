import { describe, expect, it } from "vitest";

import {
  aggregateOwnerDashboard,
  type OwnerDashboardRecord,
} from "@/domain/supervision/dashboard";

const NOW = "2026-08-30T12:00:00.000Z";

function record(
  overrides: Partial<OwnerDashboardRecord> & Pick<OwnerDashboardRecord, "id">,
): OwnerDashboardRecord {
  return {
    tenantId: "tenant-a",
    customerId: `customer-${overrides.id}`,
    customerNumber: `C-${overrides.id}`,
    customerAlias: `客户-${overrides.id}`,
    employeeUserId: "employee-a",
    employeeLabel: "员工 A",
    category: "沉香",
    submittedAt: "2026-08-30T08:00:00.000Z",
    plannedFollowUpAt: "2026-08-30T09:00:00.000Z",
    result: "pending",
    replyUsage: "not_used",
    nextAction: "人工跟进",
    decisionCardId: `card-${overrides.id}`,
    ...overrides,
    id: overrides.id,
  };
}

const fixtureRecords: OwnerDashboardRecord[] = [
  record({
    id: "follow-up-1",
    actualFollowUpAt: "2026-08-30T09:30:00.000Z",
    result: "won",
    replyUsage: "adopted",
  }),
  record({
    id: "follow-up-2",
    submittedAt: "2026-08-30T09:00:00.000Z",
    plannedFollowUpAt: "2026-08-30T10:00:00.000Z",
    replyUsage: "edited",
  }),
  record({
    id: "follow-up-3",
    submittedAt: "2026-08-28T08:00:00.000Z",
    plannedFollowUpAt: "2026-08-28T09:00:00.000Z",
    actualFollowUpAt: "2026-08-28T10:00:00.000Z",
  }),
  record({
    id: "follow-up-4",
    submittedAt: "2026-08-30T11:00:00.000Z",
    plannedFollowUpAt: "2026-08-31T09:00:00.000Z",
    result: "interested",
  }),
  record({
    id: "follow-up-5",
    employeeUserId: "employee-b",
    employeeLabel: "员工 B",
    result: "lost",
    actualFollowUpAt: "2026-08-30T08:30:00.000Z",
    replyUsage: "adopted",
  }),
];

describe("owner dashboard aggregation", () => {
  it("counts every execution and result metric by employee with exact record IDs", () => {
    const dashboard = aggregateOwnerDashboard(fixtureRecords, NOW);

    expect(dashboard.employees[0]).toMatchObject({
      employeeUserId: "employee-a",
      employeeLabel: "员工 A",
      submitted: 4,
      followedUp: 2,
      pending: 2,
      due: 3,
      overdue: 1,
      interested: 1,
      won: 1,
      lost: 0,
      adopted: 1,
      edited: 1,
      notUsed: 2,
    });
    expect(dashboard.employees[0]?.drillDownRecordIds).toEqual({
      submitted: ["follow-up-1", "follow-up-2", "follow-up-3", "follow-up-4"],
      followedUp: ["follow-up-1", "follow-up-3"],
      pending: ["follow-up-2", "follow-up-3"],
      due: ["follow-up-1", "follow-up-2", "follow-up-3"],
      overdue: ["follow-up-2"],
      interested: ["follow-up-4"],
      won: ["follow-up-1"],
      lost: [],
      adopted: ["follow-up-1"],
      edited: ["follow-up-2"],
      notUsed: ["follow-up-3", "follow-up-4"],
      missingResult: ["follow-up-3"],
      averageResponseDurationMs: ["follow-up-1", "follow-up-3"],
      completionRate: ["follow-up-1", "follow-up-3"],
    });
  });

  it("calculates response duration and due-work completion rate deterministically", () => {
    const dashboard = aggregateOwnerDashboard(fixtureRecords, NOW);
    const employeeA = dashboard.employees[0];

    expect(employeeA?.averageResponseDurationMs).toBe(6_300_000);
    expect(employeeA?.completionRate).toBeCloseTo(66.67, 2);
    expect(dashboard.totals.submitted).toBe(5);
    expect(dashboard.totals.drillDownRecordIds.submitted).toEqual([
      "follow-up-1",
      "follow-up-2",
      "follow-up-3",
      "follow-up-4",
      "follow-up-5",
    ]);
  });

  it("returns only de-identified drill-down rows in stable order", () => {
    const dashboard = aggregateOwnerDashboard([...fixtureRecords].reverse(), NOW);

    expect(dashboard.records.map((entry) => entry.id)).toEqual([
      "follow-up-1",
      "follow-up-2",
      "follow-up-3",
      "follow-up-4",
      "follow-up-5",
    ]);
    expect(JSON.stringify(dashboard.records)).not.toMatch(
      /customerFingerprint|safetyFlags|retentionFailed|vault|customerMessage|image/i,
    );
  });

  it("measures response from customer intake even when the follow-up is recorded afterward", () => {
    const dashboard = aggregateOwnerDashboard([
      record({
        id: "follow-up-recorded-later",
        responseStartedAt: "2026-08-30T08:00:00.000Z",
        actualFollowUpAt: "2026-08-30T10:00:00.000Z",
        submittedAt: "2026-08-30T11:00:00.000Z",
      }),
    ], NOW);

    expect(dashboard.totals.averageResponseDurationMs).toBe(7_200_000);
  });

  it("does not count a future actual-follow-up timestamp as completed", () => {
    const dashboard = aggregateOwnerDashboard([
      record({
        id: "follow-up-future-actual",
        actualFollowUpAt: "2026-08-30T12:00:00.001Z",
        plannedFollowUpAt: "2026-08-30T11:00:00.000Z",
      }),
    ], NOW);

    expect(dashboard.totals).toMatchObject({
      followedUp: 0,
      due: 1,
      overdue: 1,
      completionRate: 0,
      missingResult: 0,
    });
    expect(dashboard.totals.drillDownRecordIds.followedUp).toEqual([]);
    expect(dashboard.totals.drillDownRecordIds.overdue).toEqual(["follow-up-future-actual"]);
    expect(dashboard.records[0]?.actualFollowUpAt).toBeUndefined();
  });
});
