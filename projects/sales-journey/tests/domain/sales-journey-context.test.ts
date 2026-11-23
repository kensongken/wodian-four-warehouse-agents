import { expect, it } from "vitest";
import {
  buildSupervisedSalesJourney,
  createJourneyDecisionRequest,
} from "@/domain/sales-journey/context";
import type { CustomerRecord } from "@/domain/supervision/contracts";
import type { TimelineEntry } from "@/server/supervision/repository";

const journey = {
  tenantId: "tenant-a",
  journeyId: "journey-1",
  customerId: "customer-1",
  subjectId: "subject-1",
  operatorId: "operator-1",
  currentStage: "evaluate" as const,
  customerCategory: "repeat_customer",
  confirmedNeeds: ["佩戴效果"],
  pendingQuestions: ["证书"],
  commitments: ["明天回复"],
  openTasks: ["核对证书"],
  recentConversation: [{ speaker: "customer" as const, text: "有证书吗？" }],
};

it("carries tenant, operator, conversation, and journey facts into generation", () => {
  const request = createJourneyDecisionRequest({
    requestId: "req-1",
    scenario: "identity_bead",
    productDescription: "翡翠身份珠",
    latestCustomerMessage: "有证书吗？",
    journey,
    evidence: [],
  });

  expect(request.tenantId).toBe("tenant-a");
  expect(request.operatorId).toBe("operator-1");
  expect(request.conversation).toHaveLength(1);
  expect(request.productDescription).toContain("当前旅程阶段：evaluate");
  expect(request.productDescription).toContain("旅程编号：journey-1");
  expect(request.productDescription).toContain("客户编号：customer-1");
  expect(request.productDescription).toContain("当前商品/服务：subject-1");
  expect(request.productDescription).toContain("客户分类：repeat_customer");
  expect(request.productDescription).toContain("承诺事项：明天回复");
  expect(request.productDescription).toContain("待办事项：核对证书");
});

it("allows only approved evidence owned by the journey tenant", () => {
  const request = createJourneyDecisionRequest({
    requestId: "req-approved",
    scenario: "identity_bead",
    productDescription: "翡翠身份珠",
    latestCustomerMessage: "有证书吗？",
    journey,
    evidence: [{
      tenantId: "tenant-a",
      id: "evidence-1",
      kind: "certificate",
      title: "证书",
      verifiedFields: ["certificate"],
      status: "approved",
    }],
  });

  expect(request.evidence).toHaveLength(1);
  expect(request.evidence[0].id).toBe("evidence-1");
});

it.each([
  ["expired", { tenantId: "tenant-a", status: "expired" as const }],
  ["cross-tenant", { tenantId: "tenant-b", status: "approved" as const }],
])("rejects %s evidence before request generation", (_label, override) => {
  expect(() => createJourneyDecisionRequest({
    requestId: "req-rejected",
    scenario: "identity_bead",
    productDescription: "翡翠身份珠",
    latestCustomerMessage: "有证书吗？",
    journey,
    evidence: [{
      tenantId: override.tenantId,
      id: "evidence-1",
      kind: "certificate",
      title: "证书",
      verifiedFields: ["certificate"],
      status: override.status,
    }],
  })).toThrow();
});

const supervisedCustomer: CustomerRecord = {
  id: "customer-safe-1",
  tenantId: "tenant-a",
  employeeUserId: "employee-internal-1",
  customerFingerprint: `fp_${"a".repeat(64)}`,
  customerNumber: "C-SAFE-1",
  customerAlias: "不应进入旅程的真实称呼",
  category: "沉香",
  plannedFollowUpAt: "2026-09-01T08:00:00.000Z",
  status: "pending",
  version: 1,
  createdAt: "2026-08-29T08:00:00.000Z",
};

const supervisedTimeline: TimelineEntry[] = [{
  kind: "follow_up",
  record: {
    id: "follow-up-1",
    tenantId: "tenant-a",
    customerId: "customer-safe-1",
    employeeUserId: "employee-internal-1",
    customerMessageVaultAssetId: "vault-secret-chat-1",
    customerMessageSummary: "RAW-HISTORY-SHOULD-NOT-ENTER-JOURNEY",
    plannedFollowUpAt: "2026-08-30T08:00:00.000Z",
    actualFollowUpAt: "2026-08-30T07:00:00.000Z",
    result: "interested",
    nextAction: "PRIVATE-HISTORY-NEXT-ACTION",
    decisionCardId: "card-old",
    replyUsage: "edited",
    finalEditedText: "RAW-HISTORICAL-CHAT",
    version: 1,
    createdAt: "2026-08-30T07:00:00.000Z",
  },
}];

function followUpEntry(input: {
  id: string;
  tenantId?: string;
  customerId?: string;
  createdAt: string;
  result: "pending" | "interested" | "won" | "lost";
}): Extract<TimelineEntry, { kind: "follow_up" }> {
  return {
    kind: "follow_up",
    record: {
      id: input.id,
      tenantId: input.tenantId ?? supervisedCustomer.tenantId,
      customerId: input.customerId ?? supervisedCustomer.id,
      employeeUserId: "employee-internal-1",
      customerMessageVaultAssetId: `vault-${input.id}`,
      customerMessageSummary: `private-${input.id}`,
      plannedFollowUpAt: "2026-09-01T08:00:00.000Z",
      actualFollowUpAt: input.createdAt,
      result: input.result,
      nextAction: `private-next-${input.id}`,
      decisionCardId: `card-${input.id}`,
      replyUsage: "not_used",
      version: 1,
      createdAt: input.createdAt,
    },
  };
}

it("derives a privacy-safe stage from server-owned customer and timeline state", () => {
  const journey = buildSupervisedSalesJourney({
    customer: supervisedCustomer,
    timeline: supervisedTimeline,
    scenario: "first_consultation",
    latestCustomerMessage: "我想先了解一下。",
    recentContext: "客户口头提到的信息还没有核对。",
    approvedEvidence: [],
  });

  expect(journey).toMatchObject({
    tenantId: "tenant-a",
    journeyId: "journey-customer-safe-1",
    customerId: "customer-safe-1",
    subjectId: "category-沉香",
    currentStage: "evaluate",
    customerCategory: "沉香",
    confirmedNeeds: [],
  });
  expect(journey.pendingQuestions).toEqual([
    "用途",
    "对象",
    "最关注的问题",
    "本次补充背景待人工确认",
  ]);
  expect(journey.recentConversation).toEqual([
    { speaker: "customer", text: "我想先了解一下。" },
  ]);

  const serialized = JSON.stringify(journey);
  for (const forbidden of [
    "employee-internal-1",
    "不应进入旅程的真实称呼",
    `fp_${"a".repeat(64)}`,
    "vault-secret-chat-1",
    "RAW-HISTORY-SHOULD-NOT-ENTER-JOURNEY",
    "PRIVATE-HISTORY-NEXT-ACTION",
    "RAW-HISTORICAL-CHAT",
    "客户口头提到的信息还没有核对。",
  ]) {
    expect(serialized).not.toContain(forbidden);
  }
});

it.each([
  ["identity_bead", "won customer", "won", []],
  ["identity_bead", "interested follow-up", "pending", supervisedTimeline],
  ["scan_service", "won customer", "won", []],
  ["scan_service", "interested follow-up", "pending", supervisedTimeline],
] as const)(
  "keeps %s in discover without required evidence despite %s progress",
  (scenario, _progress, status, timeline) => {
    const journey = buildSupervisedSalesJourney({
      customer: { ...supervisedCustomer, status },
      timeline,
      scenario,
      latestCustomerMessage: "请先介绍一下。",
      approvedEvidence: [],
    });

    expect(journey.currentStage).toBe("discover");
  },
);

it("selects the latest same-customer follow-up by timestamp then stable record id", () => {
  const equalTimestamp = "2026-08-30T09:00:00.000Z";
  const journey = buildSupervisedSalesJourney({
    customer: supervisedCustomer,
    timeline: [
      followUpEntry({
        id: "follow-up-earlier",
        createdAt: "2026-08-30T08:00:00.000Z",
        result: "interested",
      }),
      followUpEntry({ id: "follow-up-a", createdAt: equalTimestamp, result: "won" }),
      followUpEntry({ id: "follow-up-z", createdAt: equalTimestamp, result: "lost" }),
      followUpEntry({
        id: "follow-up-wrong-customer",
        customerId: "customer-other",
        createdAt: "2026-08-30T10:00:00.000Z",
        result: "won",
      }),
      followUpEntry({
        id: "follow-up-wrong-tenant",
        tenantId: "tenant-other",
        createdAt: "2026-08-30T11:00:00.000Z",
        result: "won",
      }),
    ],
    scenario: "first_consultation",
    latestCustomerMessage: "我再考虑一下。",
    approvedEvidence: [],
  });

  expect(journey.currentStage).toBe("follow_up");
});

it("keeps unknown scenario facts pending and rejects cross-tenant evidence", () => {
  expect(() => buildSupervisedSalesJourney({
    customer: supervisedCustomer,
    timeline: [],
    scenario: "identity_bead",
    latestCustomerMessage: "这个身份珠有什么用？",
    approvedEvidence: [{
      tenantId: "tenant-b",
      id: "evidence-cross-tenant",
      kind: "approved_copy",
      title: "他店资料",
      verifiedFields: ["identity_bead"],
      status: "approved",
    }],
  })).toThrow("belongs to another tenant");

  const journey = buildSupervisedSalesJourney({
    customer: supervisedCustomer,
    timeline: [],
    scenario: "identity_bead",
    latestCustomerMessage: "这个身份珠有什么用？",
    approvedEvidence: [],
  });
  const request = createJourneyDecisionRequest({
    requestId: "request-supervised-unknowns",
    operatorId: "employee-internal-1",
    scenario: "identity_bead",
    productDescription: "当前身份珠咨询",
    latestCustomerMessage: "这个身份珠有什么用？",
    journey,
    evidence: [],
  });

  expect(journey.currentStage).toBe("discover");
  expect(journey.pendingQuestions).toEqual(["最关心的部分"]);
  expect(request.productDescription).toContain("待确认：最关心的部分");
});
