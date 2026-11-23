import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

const BASE_URL = "http://127.0.0.1:3000";

const IDENTITIES = {
  employee1: {
    id: "employee-1",
    email: "employee-1@shop.test",
  },
  employee2: {
    id: "employee-2",
    email: "employee-2@shop.test",
  },
  owner: {
    id: "owner-1",
    email: "owner@shop.test",
  },
} as const;

async function authenticatedContext(
  browser: Browser,
  identity: (typeof IDENTITIES)[keyof typeof IDENTITIES],
): Promise<BrowserContext> {
  return browser.newContext({
    baseURL: BASE_URL,
    extraHTTPHeaders: {
      "oai-authenticated-user-id": identity.id,
      "oai-authenticated-user-email": identity.email,
    },
  });
}

async function createSyntheticCustomer(page: Page, alias: string, realName: string): Promise<string> {
  await page.goto("/employee/customers/new");
  await expect(page.getByRole("heading", {
    level: 1,
    name: "建立客户档案",
    exact: true,
  })).toBeVisible();
  await page.getByLabel("客户称呼").fill(alias);
  await page.getByLabel("真实姓名（仅存保险柜，可选）").fill(realName);
  await page.getByLabel("计划跟进时间").fill("2026-09-02T10:00");
  await page.getByRole("button", { name: "建立客户档案" }).click();
  await expect(page.getByRole("status")).toHaveText("已同步");
  const followUpLink = page.getByRole("link", { name: "进入客户跟进" });
  const href = await followUpLink.getAttribute("href");
  expect(href).toMatch(/^\/employee\/customers\/[^/]+$/u);
  return decodeURIComponent(href!.split("/").at(-1)!);
}

test("employee records a private customer and another employee cannot read it", async ({ browser }) => {
  const suffix = crypto.randomUUID().slice(0, 8);
  const alias = `员工验收-${suffix}`;
  const realName = `真实姓名-${suffix}`;
  const employeeOne = await authenticatedContext(browser, IDENTITIES.employee1);
  const employeeTwo = await authenticatedContext(browser, IDENTITIES.employee2);
  try {
    const page = await employeeOne.newPage();
    const customerId = await createSyntheticCustomer(page, alias, realName);

    await page.goto("/employee/customers");
    await expect(page.getByText(alias)).toBeVisible();
    await expect(page.getByText(realName)).toHaveCount(0);

    const crossAccess = await employeeTwo.request.get(
      `/api/supervision/customers/${encodeURIComponent(customerId)}/follow-ups`,
    );
    expect(crossAccess.status()).toBe(404);

    const employeeOnOwnerApi = await employeeOne.request.get(
      "/api/supervision/owner/dashboard",
    );
    expect(employeeOnOwnerApi.status()).toBe(403);
  } finally {
    await employeeOne.close();
    await employeeTwo.close();
  }
});

test("employee can evaluate an edited reply alongside an independently saved follow-up", async ({ browser }) => {
  const suffix = crypto.randomUUID().slice(0, 8);
  const employee = await authenticatedContext(browser, IDENTITIES.employee1);
  try {
    const page = await employee.newPage();
    const customerId = await createSyntheticCustomer(page, `复盘验收-${suffix}`, `合成姓名-${suffix}`);

    await page.goto(`/employee/customers/${encodeURIComponent(customerId)}`);
    await page.getByLabel("商品描述").fill("身份珠");
    await page.getByLabel("客户问题").fill("这个身份珠有什么用？");
    await page.getByRole("button", { name: "生成销售建议" }).click();
    await expect(page.getByRole("heading", { name: "销售建议草稿" })).toBeVisible();

    await page.getByLabel("选择第 1 条回复").check();
    await page.getByLabel("已改写").check();
    await page.getByLabel("最终回复").fill("我先帮您核对资料，再为您说明。");
    await page.getByLabel("下一步动作").fill("明天下午确认到店时间");

    await page.getByRole("button", { name: "保存并同步给老板" }).click();
    await expect(page.getByRole("status").filter({ hasText: "已同步" })).toBeVisible();

    await page.getByRole("button", { name: "👎 需要改进" }).click();
    await page.getByLabel("太长").check();
    await expect(page.getByLabel("最终回复（可选）")).toHaveValue("我先帮您核对资料，再为您说明。");
    await page.getByRole("button", { name: "提交评价" }).click();
    await expect(page.getByRole("status").filter({ hasText: "感谢评价" })).toBeVisible();

    await expect(page.getByRole("button", { name: /发送|自动发送|微信发送/u })).toHaveCount(0);
  } finally {
    await employee.close();
  }
});
