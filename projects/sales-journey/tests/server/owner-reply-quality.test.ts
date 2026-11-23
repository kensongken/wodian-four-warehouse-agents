import { describe, expect, it } from "vitest";

import type {
  OwnerReplyQualityQuery,
  ReplyFeedbackRecord,
} from "@/domain/supervision/contracts";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import {
  getOwnerReplyQualityView,
  type OwnerDirectoryView,
} from "@/server/supervision/owner-view";

const feedbackBase = {
  decisionCardId: "card-a",
  cardDigest: `sha256_${"a".repeat(64)}`,
  agentVersion: "reply-agent.v2",
  scenario: "product_selection",
  salesStage: "evaluate",
  evidenceIds: ["evidence-a"],
  createdAt: "2026-08-30T08:00:00.000Z",
} as const;

const feedback: ReplyFeedbackRecord[] = [
  {
    ...feedbackBase,
    id: "feedback-safe-a",
    tenantId: "tenant-a",
    employeeUserId: "employee-a",
    customerId: "customer-a",
    cardInstanceId: "instance-safe-a",
    rating: "helpful",
    reasonCodes: [],
    privacyStatus: "safe",
    privacyReasonCodes: [],
    sanitizedFinalReply: "我先把已核实的纹理信息整理给您。",
    updatedAt: "2026-08-30T08:03:00.000Z",
  },
  {
    ...feedbackBase,
    id: "feedback-quarantined-a",
    tenantId: "tenant-a",
    employeeUserId: "employee-a",
    customerId: "customer-a",
    cardInstanceId: "instance-quarantined-a",
    agentVersion: "reply-agent.v3",
    rating: "needs_improvement",
    reasonCodes: ["too_long", "wrong_stage"],
    privacyStatus: "quarantined",
    privacyReasonCodes: ["residual_phone"],
    updatedAt: "2026-08-30T08:02:00.000Z",
  },
  {
    ...feedbackBase,
    id: "feedback-other-tenant",
    tenantId: "tenant-b",
    employeeUserId: "employee-b",
    customerId: "customer-b",
    cardInstanceId: "instance-b",
    rating: "needs_improvement",
    reasonCodes: ["other"],
    privacyStatus: "safe",
    privacyReasonCodes: [],
    sanitizedFinalReply: "OTHER_TENANT_REPLY",
    updatedAt: "2026-08-30T08:04:00.000Z",
  },
];

const directory: OwnerDirectoryView = {
  employees: [{
    userId: "employee-a",
    navigationId: "employee-view-safe-a",
    displayLabel: "employee-a@shop.test",
  }],
  customers: [{
    id: "customer-a",
    navigationId: "customer-view-safe-a",
    customerNumber: "C-018",
    customerAlias: "林女士",
    employeeUserId: "employee-a",
    category: "沉香",
    plannedFollowUpAt: "2026-08-30T10:00:00.000Z",
    status: "pending",
  }],
};

describe("owner reply-quality view", () => {
  it("aggregates only the requested tenant and replaces internal identities with directory refs", async () => {
    const repository = new MemorySupervisionRepository({ seed: { replyFeedback: feedback } });

    const view = await getOwnerReplyQualityView(repository, directory, {
      tenantId: "tenant-a",
    });

    expect(view.totals).toEqual({ helpful: 1, needsImprovement: 1, quarantined: 1 });
    expect(view.reasons).toEqual([
      { code: "too_long", count: 1 },
      { code: "wrong_stage", count: 1 },
    ]);
    expect(view.samples).toHaveLength(2);
    expect(view.samples[0]).toMatchObject({
      feedbackRef: "feedback-row-1",
      employeeRef: "employee-view-safe-a",
      customerRef: "customer-view-safe-a",
      privacyStatus: "safe",
      sanitizedFinalReply: "我先把已核实的纹理信息整理给您。",
    });
    expect(JSON.stringify(view)).not.toMatch(
      /tenant-a|employee-a(?!@)|customer-a|feedback-safe-a|OTHER_TENANT_REPLY/u,
    );
  });

  it("omits the reply property entirely for quarantined samples", async () => {
    const repository = new MemorySupervisionRepository({ seed: { replyFeedback: feedback } });

    const view = await getOwnerReplyQualityView(repository, directory, {
      tenantId: "tenant-a",
      agentVersion: "reply-agent.v3",
    } satisfies OwnerReplyQualityQuery);

    expect(view.samples).toEqual([expect.objectContaining({
      privacyStatus: "quarantined",
      privacyReasonCodes: ["residual_phone"],
    })]);
    expect(view.samples[0]).not.toHaveProperty("sanitizedFinalReply");
  });
});
