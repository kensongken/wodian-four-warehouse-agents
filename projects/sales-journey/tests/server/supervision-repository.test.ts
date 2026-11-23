import { describe, expect, it } from "vitest";

import type {
  CustomerRecord,
  DecisionUsageRecord,
  FollowUpRecord,
  VaultAssetRecord,
} from "@/domain/supervision/contracts";
import {
  MemorySupervisionRepository,
  seededMemorySupervisionRepository,
} from "@/server/supervision/memory-repository";

const CREATED_AT = "2026-08-30T06:30:00.000Z";

const customerA: CustomerRecord = {
  id: "customer-a",
  tenantId: "tenant-a",
  employeeUserId: "employee-a",
  customerNumber: "C-001",
  customerAlias: "林女士",
  category: "沉香",
  plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
  status: "pending",
  version: 1,
  createdAt: CREATED_AT,
};

const customerB: CustomerRecord = {
  ...customerA,
  id: "customer-b",
  customerNumber: "C-002",
  customerAlias: "陈女士",
};

const followUpA: FollowUpRecord = {
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
  createdAt: CREATED_AT,
};

describe("memory supervision repository", () => {
  it("never returns another tenant's customer", async () => {
    const repo = seededMemorySupervisionRepository();

    await expect(repo.getCustomer({
      tenantId: "tenant-b",
      customerId: "customer-a",
    })).resolves.toBeUndefined();
  });

  it("appends a correction without replacing the original follow-up", async () => {
    const repo = seededMemorySupervisionRepository();

    await repo.appendCorrection({
      tenantId: "tenant-a",
      followUpId: "f-1",
      reason: "时间更正",
      actorUserId: "u-1",
    });

    const timeline = await repo.listTimeline({
      tenantId: "tenant-a",
      customerId: "customer-a",
    });
    expect(timeline.map((entry) => entry.kind)).toEqual(["follow_up", "correction"]);
    expect(timeline[0]).toMatchObject({ kind: "follow_up", record: followUpA });
  });

  it("does not append a correction to another tenant's follow-up", async () => {
    const repo = seededMemorySupervisionRepository();

    await expect(repo.appendCorrection({
      tenantId: "tenant-b",
      followUpId: "f-1",
      reason: "越权更正",
      actorUserId: "u-2",
    })).rejects.toThrow("follow-up not found");
  });

  it("rejects decision usage whose customer differs from its follow-up", async () => {
    const repo = new MemorySupervisionRepository({
      seed: { customers: [customerA, customerB], followUps: [followUpA] },
    });

    await expect(repo.appendDecisionUsage({
      tenantId: "tenant-a",
      customerId: "customer-b",
      followUpId: "f-1",
      employeeUserId: "employee-a",
      decisionCardId: "card-001",
      scenario: "silent_customer_followup",
      deliveryMode: "copy_only",
      providedReplyIds: ["reply-01"],
      replyUsage: "not_used",
    })).rejects.toThrow("follow-up customer mismatch");
  });

  it("rejects an arbitrary vault expiry at the create boundary", async () => {
    const repo = new MemorySupervisionRepository({
      seed: { customers: [customerA] },
    });

    await expect(repo.createVaultAsset({
      tenantId: "tenant-a",
      customerId: "customer-a",
      uploadedByUserId: "employee-a",
      kind: "chat_text",
      vaultKey: "tenant-a/vault-001",
      uploadedAt: "2026-08-30T06:30:00.000Z",
      deleteAfter: "2026-09-28T06:30:00.000Z",
    })).rejects.toMatchObject({ name: "ZodError" });
  });

  it("lists only the tenant's expired vault assets after schema validation", async () => {
    const tenantAAsset: VaultAssetRecord = {
      id: "vault-a",
      tenantId: "tenant-a",
      customerId: "customer-a",
      uploadedByUserId: "employee-a",
      kind: "chat_text",
      vaultKey: "tenant-a/vault-a",
      uploadedAt: "2026-08-01T00:00:00.000Z",
      deleteAfter: "2026-08-31T00:00:00.000Z",
      version: 1,
      createdAt: "2026-08-01T00:00:00.000Z",
    };
    const tenantBAsset: VaultAssetRecord = {
      ...tenantAAsset,
      id: "vault-b",
      tenantId: "tenant-b",
      customerId: "customer-b",
      vaultKey: "tenant-b/vault-b",
    };
    const repo = new MemorySupervisionRepository({
      seed: { vaultAssets: [tenantAAsset, tenantBAsset] },
    });

    await expect(repo.listExpiredVaultAssets({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
      limit: 10,
    })).resolves.toEqual([tenantAAsset]);
  });

  it("upserts one alert identity when customer and employee are absent", async () => {
    const ids = ["alert-1", "alert-2"];
    const repo = new MemorySupervisionRepository({
      createId: () => ids.shift() ?? "unexpected-id",
    });

    await repo.upsertAlert({
      tenantId: "tenant-a",
      detectedByUserId: null,
      kind: "retention_failed",
      status: "open",
    });
    await repo.upsertAlert({
      tenantId: "tenant-a",
      detectedByUserId: null,
      kind: "retention_failed",
      status: "open",
      resolutionNote: "第二次检测",
    });

    const dashboard = await repo.getOwnerDashboard({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
    });
    expect(dashboard.alerts).toEqual([
      expect.objectContaining({
        id: "alert-1",
        version: 2,
        resolutionNote: "第二次检测",
      }),
    ]);
  });

  it("detects duplicate registrations without follow-ups and regardless of active filters", async () => {
    const fingerprint = `fp_${"a".repeat(64)}`;
    const repo = new MemorySupervisionRepository({
      seed: {
        customers: [
          { ...customerA, customerFingerprint: fingerprint },
          {
            ...customerB,
            employeeUserId: "employee-b",
            customerFingerprint: fingerprint,
          },
        ],
      },
    });

    const dashboard = await repo.getOwnerDashboard({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
      from: "2026-09-01T00:00:00.000Z",
      employeeUserId: "employee-no-match",
      result: "won",
    });

    expect(dashboard.records).toEqual([]);
    expect(dashboard.riskSignals).toEqual([
      expect.objectContaining({
        kind: "duplicate_customer",
        recordIds: ["customer-a", "customer-b"],
      }),
    ]);
  });

  it("attaches duplicate-customer labels by customer ID while other risks stay follow-up scoped", async () => {
    const fingerprint = `fp_${"c".repeat(64)}`;
    const followUpB: FollowUpRecord = {
      ...followUpA,
      id: "f-2",
      customerId: "customer-b",
      employeeUserId: "employee-b",
      customerMessageVaultAssetId: "vault-follow-up-b",
      decisionCardId: "card-002",
    };
    const repo = new MemorySupervisionRepository({
      seed: {
        customers: [
          { ...customerA, customerFingerprint: fingerprint },
          {
            ...customerB,
            employeeUserId: "employee-b",
            customerFingerprint: fingerprint,
          },
        ],
        followUps: [followUpA, followUpB],
      },
    });

    const dashboard = await repo.getOwnerDashboard({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
    });

    expect(dashboard.riskSignals.find((signal) => signal.kind === "duplicate_customer"))
      .toMatchObject({ recordIds: ["customer-a", "customer-b"] });
    expect(dashboard.riskSignals
      .filter((signal) => signal.kind === "overdue_follow_up")
      .flatMap((signal) => signal.recordIds)
      .sort()).toEqual(["f-1", "f-2"]);
    expect(dashboard.records).toEqual([
      expect.objectContaining({
        id: "f-1",
        riskLabels: ["duplicate_customer", "overdue_follow_up"],
      }),
      expect.objectContaining({
        id: "f-2",
        riskLabels: ["duplicate_customer", "overdue_follow_up"],
      }),
    ]);
  });

  it("unions review-required safety rules independently of decision-usage order", async () => {
    const usage = (
      id: string,
      safetyRuleIds: DecisionUsageRecord["safetyRuleIds"],
      safetyStatus: DecisionUsageRecord["safetyStatus"] = "reviewRequired",
    ): DecisionUsageRecord => ({
      id,
      tenantId: "tenant-a",
      customerId: "customer-a",
      followUpId: "f-1",
      employeeUserId: "employee-a",
      decisionCardId: "card-001",
      scenario: "silent_customer_followup",
      deliveryMode: "copy_only",
      providedReplyIds: ["reply-01"],
      replyUsage: "not_used",
      safetyStatus,
      safetyRuleIds,
      version: 1,
      createdAt: CREATED_AT,
    });
    const usages = [
      usage("usage-price", ["PRICE_OR_PAYMENT_HUMAN_REVIEW"]),
      usage("usage-dispute", ["DEFECT_OR_DISPUTE_HUMAN_REVIEW"]),
      usage("usage-safe", ["COPY_ONLY_DELIVERY"], "safe"),
    ];
    const dashboards = await Promise.all([
      new MemorySupervisionRepository({
        seed: { customers: [customerA], followUps: [followUpA], decisionUsages: usages },
      }).getOwnerDashboard({ tenantId: "tenant-a", now: CREATED_AT }),
      new MemorySupervisionRepository({
        seed: { customers: [customerA], followUps: [followUpA], decisionUsages: [...usages].reverse() },
      }).getOwnerDashboard({ tenantId: "tenant-a", now: CREATED_AT }),
    ]);

    const labels = dashboards.map((dashboard) => dashboard.riskSignals
      .find((signal) => signal.kind === "sales_safety")?.labels);
    expect(labels).toEqual([
      ["DEFECT_OR_DISPUTE_HUMAN_REVIEW", "PRICE_OR_PAYMENT_HUMAN_REVIEW"],
      ["DEFECT_OR_DISPUTE_HUMAN_REVIEW", "PRICE_OR_PAYMENT_HUMAN_REVIEW"],
    ]);
  });

  it("normalizes negative and oversized expired-vault limits", async () => {
    const vaultAssets: VaultAssetRecord[] = Array.from({ length: 501 }, (_, index) => ({
      id: `vault-${index}`,
      tenantId: "tenant-a",
      customerId: "customer-a",
      uploadedByUserId: "employee-a",
      kind: "chat_text",
      vaultKey: `tenant-a/vault-${index}`,
      uploadedAt: "2026-08-01T00:00:00.000Z",
      deleteAfter: "2026-08-31T00:00:00.000Z",
      version: 1,
      createdAt: "2026-08-01T00:00:00.000Z",
    }));
    const repo = new MemorySupervisionRepository({ seed: { vaultAssets } });

    await expect(repo.listExpiredVaultAssets({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
      limit: -1,
    })).resolves.toEqual([]);
    await expect(repo.listExpiredVaultAssets({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
      limit: 999,
    })).resolves.toHaveLength(500);
  });
});
