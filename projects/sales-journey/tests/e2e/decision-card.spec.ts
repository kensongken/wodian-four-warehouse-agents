import { expect, test } from "@playwright/test";

import { createDecisionCard } from "../../src/domain/decision-card/advisor";
import { DecisionCardRequestSchema } from "../../src/domain/decision-card/schema";

test.beforeEach(async ({ page }) => {
  await page.route("**/api/decision-cards", async (route) => {
    const employeeInput = route.request().postDataJSON() as Record<string, unknown>;
    const request = DecisionCardRequestSchema.parse({
      ...employeeInput,
      tenantId: "tenant-e2e",
      operatorId: "employee-e2e",
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ card: createDecisionCard(request) }),
    });
  });
});

test("展示需人工复核且仅供复制的受控再利用或送回决策卡", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("业务大类").selectOption("consignment_and_return");
  await page.getByLabel("具体场景").selectOption("controlled_reuse_or_return");
  await page.getByLabel("商品描述").fill("客户想把一串沉香送回处理");
  await page.getByLabel("客户最新消息").fill("能固定按 3000 元处理吗？");
  await page.getByRole("button", { name: "生成决策卡" }).click();

  await expect(page.getByText("需人工复核")).toBeVisible();
  await expect(page.getByText("固定 3000")).not.toBeVisible();
  await expect(page.getByRole("button", { name: "复制回复" })).toBeVisible();
  await expect(page.getByRole("button", { name: /发送到微信|自动发送/ })).toHaveCount(0);
});

test("切换到重新镶嵌后仍生成需人工复核的复制草稿", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("业务大类").selectOption("customization_and_adjustment");
  await page.getByLabel("具体场景").selectOption("resetting");
  await page.getByLabel("商品描述").fill("客户的彩宝戒指");
  await page.getByLabel("客户最新消息").fill("重新镶嵌后能保证效果吗？");
  await page.getByRole("button", { name: "生成决策卡" }).click();

  await expect(page.getByText("需人工复核")).toBeVisible();
  await expect(
    page.getByRole("region", { name: "生成的销售决策卡" }).getByText("保证效果"),
  ).not.toBeVisible();
  await expect(page.getByRole("button", { name: "复制回复" })).toBeVisible();
  await expect(page.getByRole("button", { name: /发送到微信|自动发送/ })).toHaveCount(0);
});

test("新增销售场景可在页面选择，并强制进入人工复核", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("业务大类").selectOption("product_and_purchase");

  await expect(page.getByLabel("具体场景")).toHaveText(/到店预约/);
  await expect(page.getByLabel("具体场景")).toHaveText(/活动\/优惠咨询/);

  await page.getByLabel("具体场景").selectOption("promotion_or_coupon");
  await page.getByLabel("商品描述").fill("演示商品");
  await page.getByLabel("客户最新消息").fill("这个活动现在怎么参加？");
  await page.getByRole("button", { name: "生成决策卡" }).click();

  const card = page.getByRole("region", { name: "生成的销售决策卡" });
  await expect(card.getByText("需人工复核")).toBeVisible();
  await expect(card.getByText("发送方式：仅供人工复制")).toBeVisible();
  await expect(card.getByRole("button", { name: "复制回复" })).toBeVisible();
  await expect(card.getByRole("button", { name: /发送到微信|自动发送/ })).toHaveCount(0);
});

test("新增资料类型可在页面选择，且仍要求人工确认", async ({ page }) => {
  await page.goto("/");

  const evidenceType = page.getByLabel("资料类型");
  await expect(evidenceType).toHaveText(/证书\/检测报告/);
  await expect(evidenceType).toHaveText(/材质与工艺说明/);
  await expect(evidenceType).toHaveText(/售后保养说明/);
  await expect(evidenceType).toHaveText(/价格与活动规则/);

  await evidenceType.selectOption("price_or_payment");
  await expect(page.getByLabel("本次选择引用服务端已批准的演示资料")).not.toBeChecked();
});

test("未人工确认的价格与活动资料不会被引用", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("商品描述").fill("演示商品");
  await page.getByLabel("客户最新消息").fill("想先了解一下商品。");
  await page.getByLabel("资料标题").fill("本月活动说明");
  await page.getByLabel("资料类型").selectOption("price_or_payment");
  await expect(page.getByLabel("本次选择引用服务端已批准的演示资料")).not.toBeChecked();
  await page.getByRole("button", { name: "生成决策卡" }).click();

  const card = page.getByRole("region", { name: "生成的销售决策卡" });
  await expect(card.getByText("本次没有自动引用资料。")).toBeVisible();
  await expect(card.getByText("本月活动说明")).toHaveCount(0);
});

const NEW_MANUAL_REVIEW_SCENARIOS = [
  ["product_and_purchase", "store_visit_appointment", "到店预约"],
  ["order_and_delivery", "deposit_or_payment", "定金/付款"],
  ["order_and_delivery", "delivery_deadline", "交付时效"],
  ["defect_and_dispute", "complaint_escalation", "投诉升级"],
  ["follow_up_and_retention", "referral_tracking", "转介绍跟进"],
] as const;

for (const [groupId, scenario, label] of NEW_MANUAL_REVIEW_SCENARIOS) {
  test(`新增入口 ${label} 在页面生成时保留人工复核`, async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("业务大类").selectOption(groupId);
    await expect(page.getByLabel("具体场景")).toHaveText(new RegExp(label));
    await page.getByLabel("具体场景").selectOption(scenario);
    await page.getByLabel("商品描述").fill("演示商品");
    await page.getByLabel("客户最新消息").fill("我想了解下一步安排。");
    await page.getByRole("button", { name: "生成决策卡" }).click();

    const card = page.getByRole("region", { name: "生成的销售决策卡" });
    await expect(card.getByText("需人工复核")).toBeVisible();
    await expect(card.getByText("发送方式：仅供人工复制")).toBeVisible();
  });
}
