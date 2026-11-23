import { describe, expect, it } from "vitest";

import { calculateVaultDeleteAfter } from "@/domain/supervision/contracts";
import type {
  AuditEvent,
  CorrectionRecord,
  CustomerRecord,
  DecisionUsageRecord,
  FollowUpRecord,
  OwnerDashboard,
  RiskAlert,
  VaultAssetRecord,
} from "@/domain/supervision/contracts";

const ISO_TIME = "2026-08-30T06:30:00.000Z";

describe("员工监督领域合同", () => {
  it("requires tenant, actor, version, and creation time on persisted records", () => {
    const customer = {
      id: "customer-001",
      tenantId: "tenant-001",
      employeeUserId: "employee-001",
      customerNumber: "C-001",
      customerAlias: "林女士",
      category: "沉香",
      plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
      status: "pending",
      version: 1,
      createdAt: ISO_TIME,
    } satisfies CustomerRecord;
    const followUp = {
      id: "follow-up-001",
      tenantId: "tenant-001",
      customerId: customer.id,
      employeeUserId: "employee-001",
      customerMessageVaultAssetId: "vault-001",
      customerMessageSummary: "客户希望进一步了解沉香细节。",
      plannedFollowUpAt: ISO_TIME,
      actualFollowUpAt: ISO_TIME,
      result: "interested",
      nextAction: "明天发送已核实的商品资料。",
      decisionCardId: "card-001",
      replyUsage: "edited",
      chosenReplyId: "reply-02",
      finalEditedText: "我明天把已核实的资料整理给您。",
      version: 1,
      createdAt: ISO_TIME,
    } satisfies FollowUpRecord;
    const decisionUsage = {
      id: "decision-usage-001",
      tenantId: "tenant-001",
      customerId: customer.id,
      followUpId: followUp.id,
      employeeUserId: "employee-001",
      decisionCardId: "card-001",
      scenario: "silent_customer_followup",
      deliveryMode: "copy_only",
      providedReplyIds: ["reply-01", "reply-02"],
      replyUsage: "edited",
      chosenReplyId: "reply-02",
      finalEditedText: "我明天把已核实的资料整理给您。",
      copiedAt: ISO_TIME,
      version: 1,
      createdAt: ISO_TIME,
    } satisfies DecisionUsageRecord;
    const correction = {
      id: "correction-001",
      tenantId: "tenant-001",
      customerId: customer.id,
      followUpId: followUp.id,
      actorUserId: "employee-001",
      reason: "实际跟进时间录入错误。",
      version: 1,
      createdAt: ISO_TIME,
    } satisfies CorrectionRecord;
    const vaultAsset = {
      id: "vault-001",
      tenantId: "tenant-001",
      customerId: customer.id,
      uploadedByUserId: "employee-001",
      kind: "chat_text",
      vaultKey: "tenant-001/vault-001",
      uploadedAt: ISO_TIME,
      deleteAfter: "2026-09-29T06:30:00.000Z",
      version: 1,
      createdAt: ISO_TIME,
    } satisfies VaultAssetRecord;
    const audit = {
      id: "audit-001",
      tenantId: "tenant-001",
      actorUserId: "employee-001",
      action: "customer.created",
      targetKind: "customer",
      targetId: customer.id,
      occurredAt: ISO_TIME,
      version: 1,
      createdAt: ISO_TIME,
    } satisfies AuditEvent;
    const alert = {
      id: "alert-001",
      tenantId: "tenant-001",
      customerId: customer.id,
      employeeUserId: "employee-001",
      detectedByUserId: null,
      kind: "overdue_follow_up",
      status: "open",
      version: 1,
      createdAt: ISO_TIME,
    } satisfies RiskAlert;

    expect([
      customer,
      followUp,
      decisionUsage,
      correction,
      vaultAsset,
      audit,
      alert,
    ]).toHaveLength(7);
  });

  it("calculates vault expiry exactly thirty days after upload", () => {
    expect(calculateVaultDeleteAfter("2026-01-31T06:30:00.000Z"))
      .toBe("2026-03-02T06:30:00.000Z");
  });

  it("exposes an owner dashboard without raw customer material", () => {
    const dashboard = {
      tenantId: "tenant-001",
      generatedAt: ISO_TIME,
      totals: {
        submitted: 4,
        followedUp: 2,
        pending: 1,
        due: 2,
        overdue: 1,
        interested: 1,
        won: 1,
        lost: 0,
        adopted: 1,
        edited: 1,
        notUsed: 2,
        missingResult: 1,
        averageResponseDurationMs: 1_000,
        completionRate: 50,
        drillDownRecordIds: {
          submitted: ["follow-up-1"],
          followedUp: ["follow-up-1"],
          pending: [],
          due: ["follow-up-1"],
          overdue: [],
          interested: [],
          won: ["follow-up-1"],
          lost: [],
          adopted: ["follow-up-1"],
          edited: [],
          notUsed: [],
          missingResult: [],
          averageResponseDurationMs: ["follow-up-1"],
          completionRate: ["follow-up-1"],
        },
      },
      employees: [
        {
          employeeUserId: "employee-001",
          employeeLabel: "employee-001",
          ...{
            submitted: 4,
            followedUp: 2,
            pending: 1,
            due: 2,
            overdue: 1,
            interested: 1,
            won: 1,
            lost: 0,
            adopted: 1,
            edited: 1,
            notUsed: 2,
            missingResult: 1,
            averageResponseDurationMs: 1_000,
            completionRate: 50,
            drillDownRecordIds: {
              submitted: ["follow-up-1"],
              followedUp: ["follow-up-1"],
              pending: [],
              due: ["follow-up-1"],
              overdue: [],
              interested: [],
              won: ["follow-up-1"],
              lost: [],
              adopted: ["follow-up-1"],
              edited: [],
              notUsed: [],
              missingResult: [],
              averageResponseDurationMs: ["follow-up-1"],
              completionRate: ["follow-up-1"],
            },
          },
        },
      ],
      records: [],
      riskSignals: [],
      alerts: [],
    } satisfies OwnerDashboard;

    expect(dashboard.employees[0]?.overdue).toBe(1);
  });
});
