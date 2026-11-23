import { describe, expect, it } from "vitest";

import type {
  CustomerRecord,
  DecisionUsageRecord,
  FollowUpRecord,
} from "@/domain/supervision/contracts";
import type { TimelineEntry } from "@/server/supervision/repository";
import {
  toEmployeeCustomerView,
  toEmployeeTimelineView,
} from "@/server/supervision/employee-view";

const NOW = "2026-08-30T08:00:00.000Z";
const customer: CustomerRecord = {
  id: "customer-1",
  tenantId: "SECRET_TENANT",
  employeeUserId: "SECRET_ACTOR",
  idempotencyKey: "SECRET_CUSTOMER_IDEMPOTENCY",
  requestFingerprint: "SECRET_CUSTOMER_FINGERPRINT",
  customerFingerprint: "SECRET_IDENTITY_FINGERPRINT",
  customerNumber: "C-0000000001",
  customerAlias: "林女士",
  category: "沉香",
  plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
  status: "interested",
  version: 7,
  createdAt: NOW,
};

const followUp: FollowUpRecord = {
  id: "follow-up-1",
  tenantId: "SECRET_TENANT",
  customerId: customer.id,
  employeeUserId: "SECRET_ACTOR",
  idempotencyKey: "SECRET_FOLLOW_UP_IDEMPOTENCY",
  requestFingerprint: "SECRET_FOLLOW_UP_FINGERPRINT",
  customerMessageVaultAssetId: "SECRET_VAULT_ID",
  customerMessageSummary: "品类：沉香；意图：了解身份珠。",
  plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
  actualFollowUpAt: NOW,
  result: "interested",
  nextAction: "明天确认到店",
  decisionCardId: "SECRET_CARD_ID",
  cardInstanceId: "SECRET_CARD_INSTANCE",
  replyUsage: "edited",
  chosenReplyId: "SECRET_REPLY_ID",
  finalEditedText: "SECRET_FINAL_TEXT",
  version: 3,
  createdAt: NOW,
};

const usage: DecisionUsageRecord = {
  id: "usage-1",
  tenantId: "SECRET_TENANT",
  customerId: customer.id,
  followUpId: followUp.id,
  employeeUserId: "SECRET_ACTOR",
  decisionCardId: "SECRET_CARD_ID",
  cardInstanceId: "SECRET_CARD_INSTANCE",
  cardDigest: `sha256_${"a".repeat(64)}`,
  scenario: "identity_bead",
  deliveryMode: "copy_only",
  providedReplyIds: ["reply-first", "SECRET_REPLY_ID"],
  replyUsage: "edited",
  chosenReplyId: "SECRET_REPLY_ID",
  finalEditedText: "SECRET_FINAL_TEXT",
  version: 4,
  createdAt: NOW,
};

const timeline: TimelineEntry[] = [
  { kind: "follow_up", record: followUp },
  { kind: "decision_usage", record: usage },
  {
    kind: "correction",
    record: {
      id: "correction-1",
      tenantId: "SECRET_TENANT",
      customerId: customer.id,
      followUpId: followUp.id,
      actorUserId: "SECRET_ACTOR",
      idempotencyKey: "SECRET_CORRECTION_IDEMPOTENCY",
      requestFingerprint: "SECRET_CORRECTION_FINGERPRINT",
      reason: "结果选错，补充说明。",
      version: 2,
      createdAt: "2026-08-30T09:00:00.000Z",
    },
  },
];

describe("employee supervision client views", () => {
  it("maps a customer to the explicit safe fields required by the employee UI", () => {
    expect(toEmployeeCustomerView(customer)).toEqual({
      id: "customer-1",
      customerNumber: "C-0000000001",
      customerAlias: "林女士",
      category: "沉香",
      plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
      status: "interested",
    });
  });

  it("serializes only safe timeline display data and derives a reply position without IDs", () => {
    const view = toEmployeeTimelineView(timeline, () => "员工小吴");
    const serialized = JSON.stringify(view);

    expect(view).toMatchObject([
      {
        kind: "follow_up",
        actorLabel: "员工小吴",
        replySelection: "第 2 条回复",
        replyUsage: "edited",
        result: "interested",
      },
      {
        kind: "decision_usage",
        actorLabel: "员工小吴",
        replySelection: "第 2 条回复",
        replyUsage: "edited",
      },
      {
        kind: "correction",
        actorLabel: "员工小吴",
        reason: "结果选错，补充说明。",
      },
    ]);
    for (const secret of [
      "SECRET_TENANT",
      "SECRET_ACTOR",
      "SECRET_CUSTOMER_IDEMPOTENCY",
      "SECRET_CUSTOMER_FINGERPRINT",
      "SECRET_IDENTITY_FINGERPRINT",
      "SECRET_FOLLOW_UP_IDEMPOTENCY",
      "SECRET_FOLLOW_UP_FINGERPRINT",
      "SECRET_VAULT_ID",
      "SECRET_CARD_ID",
      "SECRET_CARD_INSTANCE",
      "SECRET_REPLY_ID",
      "SECRET_FINAL_TEXT",
      "SECRET_CORRECTION_IDEMPOTENCY",
      "SECRET_CORRECTION_FINGERPRINT",
      usage.cardDigest!,
      "reply-first",
    ]) {
      expect(serialized).not.toContain(secret);
    }
    expect(serialized).not.toContain("providedReplyIds");
    expect(serialized).not.toContain("version");
  });
});
