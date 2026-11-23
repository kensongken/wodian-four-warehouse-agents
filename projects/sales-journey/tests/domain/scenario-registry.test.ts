import { describe, expect, it } from "vitest";

import {
  GLOBAL_FALLBACK_SCENARIO,
  SCENARIO_DEFINITIONS,
  SCENARIO_GROUPS,
  scenariosForGroup,
} from "@/domain/decision-card/scenarios/catalog";

const expectedCounts = {
  product_and_purchase: 10,
  order_and_delivery: 8,
  customization_and_adjustment: 7,
  defect_and_dispute: 7,
  after_sales_and_care: 6,
  identity_and_scan: 4,
  consignment_and_return: 8,
  follow_up_and_retention: 6,
} as const;

describe("珠宝销售场景目录", () => {
  it("注册八大类、56 个业务场景和 1 个全局兜底", () => {
    expect(SCENARIO_GROUPS).toHaveLength(8);
    expect(SCENARIO_DEFINITIONS).toHaveLength(57);
    expect(new Set(SCENARIO_DEFINITIONS.map((item) => item.id)).size).toBe(57);
    expect(GLOBAL_FALLBACK_SCENARIO).toMatchObject({
      id: "other_needs_review",
      groupId: null,
      alwaysReview: true,
      defaultVerificationField: "service_policy",
    });

    for (const [groupId, count] of Object.entries(expectedCounts)) {
      expect(scenariosForGroup(groupId as keyof typeof expectedCounts)).toHaveLength(count);
    }
  });

  it("每个定义都有可直接生成卡片的完整内容", () => {
    for (const definition of SCENARIO_DEFINITIONS) {
      expect(definition.label.trim()).not.toBe("");
      expect(definition.description.trim()).not.toBe("");
      expect(definition.requiredInputs.length).toBeGreaterThan(0);
      expect(definition.replyFrame.trim()).not.toBe("");
      expect(definition.nextAction.text.trim()).not.toBe("");
    }
  });

  it("保留四个旧场景 ID", () => {
    const ids = SCENARIO_DEFINITIONS.map((item) => item.id);
    expect(ids).toEqual(expect.arrayContaining([
      "identity_bead",
      "scan_service",
      "swap_or_redesign",
      "controlled_reuse_or_return",
    ]));
  });

  it("为前台销售补齐六个需要人工核对的细分入口", () => {
    const additions = [
      "store_visit_appointment",
      "promotion_or_coupon",
      "deposit_or_payment",
      "delivery_deadline",
      "complaint_escalation",
      "referral_tracking",
    ];

    expect(SCENARIO_DEFINITIONS).toEqual(expect.arrayContaining(
      additions.map((id) => expect.objectContaining({ id, alwaysReview: true })),
    ));
  });
});
