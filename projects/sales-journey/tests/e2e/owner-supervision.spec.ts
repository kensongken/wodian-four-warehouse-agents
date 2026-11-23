import { expect, test, type Browser, type BrowserContext } from "@playwright/test";

const BASE_URL = "http://127.0.0.1:3000";
const RAW_PHONE = "13800138000";
const SAFE_OWNER_REPLY = "我先把已核实的身份珠资料整理给您。";
const OTHER_TENANT_REPLY = "另一家店的合成安全回复。";
const QUARANTINED_RAW = "邮箱 alice @ example.com";

async function authenticatedContext(
  browser: Browser,
  id: string,
  email: string,
): Promise<BrowserContext> {
  return browser.newContext({
    baseURL: BASE_URL,
    extraHTTPHeaders: {
      "oai-authenticated-user-id": id,
      "oai-authenticated-user-email": email,
    },
  });
}

async function createFeedback(
  context: BrowserContext,
  alias: string,
  finalReply: string,
  expectedPrivacy: "safe" | "quarantined",
): Promise<void> {
  const page = await context.newPage();
  await page.goto("/employee/customers/new");
  await page.getByLabel("客户称呼").fill(alias);
  await page.getByLabel("计划跟进时间").fill("2026-09-02T10:00");
  await page.getByRole("button", { name: "建立客户档案" }).click();
  await expect(page.getByRole("status")).toHaveText("已同步");
  const href = await page.getByRole("link", { name: "进入客户跟进" }).getAttribute("href");
  expect(href).toMatch(/^\/employee\/customers\/[^/]+$/u);
  await page.goto(href!);
  await page.getByLabel("商品描述").fill("脱敏合成商品");
  await page.getByLabel("客户问题").fill("请说明身份珠服务");
  await page.getByRole("button", { name: "生成销售建议" }).click();
  await expect(page.getByRole("heading", { name: "销售建议草稿" })).toBeVisible();
  await page.getByRole("button", { name: "👎 需要改进" }).click();
  await page.getByLabel("太长").check();
  await page.getByLabel("最终回复（可选）").fill(finalReply);
  await page.getByRole("button", { name: "提交评价" }).click();
  if (expectedPrivacy === "safe") {
    await expect(page.getByRole("status").filter({ hasText: "感谢评价" })).toBeVisible();
  } else {
    await expect(page.getByRole("alert").filter({ hasText: "回复文本没有保存" })).toBeVisible();
  }
  await page.close();
}

test("employee records a follow-up and owner sees the exact safe ledger entry", async ({ browser }) => {
  test.setTimeout(60_000);
  const suffix = crypto.randomUUID().slice(0, 8);
  const alias = `老板验收-${suffix}`;
  const realName = `真实姓名-${suffix}`;
  const nextAction = `回访动作-${suffix}`;
  const employee = await authenticatedContext(
    browser,
    "employee-1",
    "employee-1@shop.test",
  );
  const owner = await authenticatedContext(browser, "owner-1", "owner@shop.test");
  try {
    const employeePage = await employee.newPage();
    await employeePage.goto("/employee/customers/new");
    await employeePage.getByLabel("客户称呼").fill(alias);
    await employeePage.getByLabel("真实姓名（仅存保险柜，可选）").fill(realName);
    await employeePage.getByLabel("计划跟进时间").fill("2026-09-02T10:00");
    await employeePage.getByRole("button", { name: "建立客户档案" }).click();
    await expect(employeePage.getByRole("status")).toHaveText("已同步");
    await Promise.all([
      employeePage.waitForURL(/\/employee\/customers\/[^/]+$/u),
      employeePage.getByRole("link", { name: "进入客户跟进" }).click(),
    ]);
    // The dev-only synthetic server compiles this dynamic route on first visit.
    // Reload once before input so Fast Refresh cannot preserve stale DOM values.
    await employeePage.reload();

    const generate = employeePage.getByRole("button", { name: "生成销售建议" });
    await expect(generate).toBeDisabled();
    await employeePage.getByLabel("商品描述").fill("脱敏演示商品");
    await employeePage.getByLabel("客户问题").fill(`客户原话 ${RAW_PHONE}`);
    await expect(generate).toBeEnabled();
    await generate.click();
    await expect(employeePage.getByRole("region", { name: "生成的销售决策卡" })).toBeVisible();
    await employeePage.getByLabel("已采用").check();
    await employeePage.getByLabel("下一步动作").fill(nextAction);
    await employeePage.getByRole("button", { name: "保存并同步给老板" }).click();
    await expect(employeePage.getByRole("status").filter({ hasText: "已同步" })).toHaveText("已同步");

    const ownerPage = await owner.newPage();
    await ownerPage.goto("/owner");
    const row = ownerPage.getByRole("row").filter({ hasText: alias });
    await expect(row).toContainText(/C-/u);
    await expect(row).toContainText(nextAction);
    await expect(ownerPage.getByText(realName)).toHaveCount(0);
    await expect(ownerPage.getByText(RAW_PHONE)).toHaveCount(0);

    const exported = await owner.request.get("/api/supervision/owner/export");
    expect(exported.status()).toBe(200);
    const csv = await exported.text();
    expect(csv).toContain(alias);
    expect(csv).not.toContain(realName);
    expect(csv).not.toContain(RAW_PHONE);
    expect(csv).not.toMatch(/vault|private\//iu);
  } finally {
    await employee.close().catch(() => undefined);
    await owner.close().catch(() => undefined);
  }
});

test("owner reply quality is tenant-scoped and quarantined samples stay text-free", async ({ browser }) => {
  test.setTimeout(90_000);
  const suffix = crypto.randomUUID().slice(0, 8);
  const tenantAEmployee = await authenticatedContext(browser, "employee-1", "employee-1@shop.test");
  const tenantAOwner = await authenticatedContext(browser, "owner-1", "owner@shop.test");
  const tenantBEmployee = await authenticatedContext(browser, "employee-3", "employee-3@other-shop.test");
  const tenantBOwner = await authenticatedContext(browser, "owner-2", "owner@other-shop.test");
  try {
    await createFeedback(tenantAEmployee, `安全样本-${suffix}`, SAFE_OWNER_REPLY, "safe");
    await createFeedback(tenantAEmployee, `隔离样本-${suffix}`, QUARANTINED_RAW, "quarantined");
    await createFeedback(tenantBEmployee, `他店样本-${suffix}`, OTHER_TENANT_REPLY, "safe");

    const ownerPage = await tenantAOwner.newPage();
    await ownerPage.goto("/owner");
    const qualityPanel = ownerPage.getByRole("heading", {
      name: "回复质量（不用于员工打分）",
    }).locator("..");
    await expect(qualityPanel.getByText(SAFE_OWNER_REPLY)).toBeVisible();
    await expect(qualityPanel.getByText(/residual_email/u)).toBeVisible();
    await expect(ownerPage.getByText(QUARANTINED_RAW)).toHaveCount(0);
    await expect(ownerPage.getByText(OTHER_TENANT_REPLY)).toHaveCount(0);

    const ownerResponse = await tenantAOwner.request.get("/api/supervision/owner/reply-quality");
    expect(ownerResponse.status()).toBe(200);
    const ownerJson = await ownerResponse.text();
    expect(ownerJson).toContain(SAFE_OWNER_REPLY);
    expect(ownerJson).not.toContain(QUARANTINED_RAW);
    expect(ownerJson).not.toContain(OTHER_TENANT_REPLY);
    expect(ownerJson).not.toMatch(/tenant-synthetic|employee-1|cardInstanceId|decisionCardId/u);

    const otherOwnerPage = await tenantBOwner.newPage();
    await otherOwnerPage.goto("/owner");
    await expect(otherOwnerPage.getByText(OTHER_TENANT_REPLY)).toBeVisible();
    await expect(otherOwnerPage.getByText(SAFE_OWNER_REPLY)).toHaveCount(0);
  } finally {
    await tenantAEmployee.close().catch(() => undefined);
    await tenantAOwner.close().catch(() => undefined);
    await tenantBEmployee.close().catch(() => undefined);
    await tenantBOwner.close().catch(() => undefined);
  }
});
