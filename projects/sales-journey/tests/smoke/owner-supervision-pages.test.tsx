import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const actor = {
    accountUserId: "owner-account-secret",
    email: "owner@shop.test",
    tenantId: "tenant-secret",
    role: "owner" as const,
  };
  const metrics = {
    submitted: 1,
    followedUp: 1,
    pending: 0,
    due: 1,
    overdue: 0,
    interested: 0,
    won: 1,
    lost: 0,
    adopted: 1,
    edited: 0,
    notUsed: 0,
    missingResult: 0,
    averageResponseDurationMs: 60_000,
    completionRate: 100,
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
  };
  const dashboard = {
    tenantId: "tenant-secret",
    generatedAt: "2026-08-30T12:00:00.000Z",
    totals: metrics,
    employees: [{
      employeeUserId: "employee-account-secret",
      employeeLabel: "employee-account-secret",
      ...metrics,
    }],
    records: [{
      id: "follow-up-1",
      customerNumber: "C-0001",
      customerAlias: "林女士",
      employeeUserId: "employee-account-secret",
      employeeLabel: "employee-account-secret",
      category: "沉香",
      submittedAt: "2026-08-30T08:00:00.000Z",
      plannedFollowUpAt: "2026-08-30T09:00:00.000Z",
      actualFollowUpAt: "2026-08-30T08:30:00.000Z",
      decisionUsage: "adopted",
      nextAction: "等待客户确认",
      result: "won",
      riskLabels: [],
    }],
    riskSignals: [],
    alerts: [],
  };
  const directory = {
    employees: [{
      userId: "employee-account-secret",
      navigationId: "employee-view-1",
      displayLabel: "小吴 · employee@shop.test",
    }],
    customers: [{
      id: "customer-1",
      navigationId: "customer-view-1",
      customerNumber: "C-0001",
      customerAlias: "林女士",
      employeeUserId: "employee-account-secret",
      category: "沉香",
      plannedFollowUpAt: "2026-08-30T09:00:00.000Z",
      status: "won",
    }],
  };
  return {
    actor,
    dashboard,
    directory,
    d1: { prepare: vi.fn() },
    requireOwnerFromD1: vi.fn(async () => actor),
    getOwnerDashboard: vi.fn(async () => dashboard),
    listTimeline: vi.fn(async () => [{
      kind: "follow_up",
      record: {
        id: "follow-up-1",
        customerMessageSummary: "客户询问保养方式",
      },
    }]),
    getOwnerDirectory: vi.fn(async () => directory),
    getOwnerReplyQualityView: vi.fn(async () => ({
      totals: { helpful: 0, needsImprovement: 0, quarantined: 0 },
      reasons: [],
      samples: [],
    })),
    getOwnerCustomerVaultAssets: vi.fn(async () => [{
      id: "asset-1",
      kind: "product_image",
      deleteAfter: "2026-09-29T08:00:00.000Z",
    }]),
    getOwnerVaultAssetView: vi.fn(async () => ({
      id: "asset-1",
      kind: "product_image",
      deleteAfter: "2026-09-29T08:00:00.000Z",
    })),
    toOwnerTimelineView: vi.fn(() => [{
      kind: "follow_up",
      id: "follow-up-1",
      actorLabel: "小吴 · employee@shop.test",
      createdAt: "2026-08-30T08:00:00.000Z",
      customerMessageSummary: "客户询问保养方式",
      plannedFollowUpAt: "2026-08-30T09:00:00.000Z",
      actualFollowUpAt: "2026-08-30T08:30:00.000Z",
      result: "won",
      nextAction: "等待客户确认",
      replyUsage: "adopted",
    }]),
    toOwnerDashboardBrowserView: vi.fn(() => ({
      generatedAt: "2026-08-30T12:00:00.000Z",
      summaryMetrics: [], records: [], risks: [], customers: [{ navigationId: "customer-view-1", customerNumber: "C-0001", customerAlias: "林女士", category: "沉香", status: "won" }],
      employees: [{ navigationId: "employee-view-1", displayLabel: "小吴 · employee@shop.test", metrics: [
        { key: "completionRate", label: "完成", valueLabel: "1/1（100%）", recordRefs: [] }, { key: "adopted", label: "采用", valueLabel: "1", recordRefs: [] },
        { key: "edited", label: "改写", valueLabel: "0", recordRefs: [] }, { key: "notUsed", label: "未用", valueLabel: "0", recordRefs: [] },
        { key: "won", label: "成交", valueLabel: "1", recordRefs: [] }, { key: "interested", label: "意向", valueLabel: "0", recordRefs: [] },
        { key: "lost", label: "未成交", valueLabel: "0", recordRefs: [] }, { key: "overdue", label: "超期", valueLabel: "0", recordRefs: [] },
      ] }],
    })),
    resolveOwnerEmployeeRef: vi.fn((view: typeof directory, ref: string) => view.employees.find((item) => item.navigationId === ref)),
    resolveOwnerCustomerRef: vi.fn((view: typeof directory, ref: string) => view.customers.find((item) => item.navigationId === ref)),
  };
});

vi.mock("next/headers", () => ({ headers: vi.fn(async () => new Headers()) }));
vi.mock("next/navigation", () => ({ notFound: vi.fn(() => { throw new Error("NOT_FOUND"); }) }));
vi.mock("@/server/auth/authorization", () => ({
  requireOwnerFromD1: mocks.requireOwnerFromD1,
}));
vi.mock("@/server/supervision/customer-service", () => ({
  getSupervisionApiRuntime: vi.fn(() => ({
    d1: mocks.d1,
    repository: {
      getOwnerDashboard: mocks.getOwnerDashboard,
      listTimeline: mocks.listTimeline,
    },
    now: () => "2026-08-30T12:00:00.000Z",
    usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
  })),
}));
vi.mock("@/server/supervision/owner-view", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/server/supervision/owner-view")>(),
  getOwnerDirectory: mocks.getOwnerDirectory,
  getOwnerReplyQualityView: mocks.getOwnerReplyQualityView,
  getOwnerCustomerVaultAssets: mocks.getOwnerCustomerVaultAssets,
  getOwnerVaultAssetView: mocks.getOwnerVaultAssetView,
  toOwnerDashboardBrowserView: mocks.toOwnerDashboardBrowserView,
  resolveOwnerEmployeeRef: mocks.resolveOwnerEmployeeRef,
  resolveOwnerCustomerRef: mocks.resolveOwnerCustomerRef,
  toOwnerTimelineView: mocks.toOwnerTimelineView,
}));

describe("owner supervision pages", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("requires explicit D1 owner authorization before rendering the execution dashboard", async () => {
    const OwnerPage = (await import("@/app/owner/page")).default;
    const tree = await OwnerPage({ searchParams: Promise.resolve({}) });
    const serialized = JSON.stringify(tree);
    render(tree);

    expect(mocks.requireOwnerFromD1).toHaveBeenCalledWith(expect.any(Headers), mocks.d1);
    expect(mocks.getOwnerDashboard).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: "tenant-secret",
      now: "2026-08-30T12:00:00.000Z",
    }));
    expect(screen.getByRole("heading", { name: "老板执行看板" })).toBeInTheDocument();
    expect(mocks.toOwnerDashboardBrowserView).toHaveBeenCalledWith(mocks.dashboard, mocks.directory);
    expect(serialized).not.toMatch(/tenant-secret|employee-account-secret|riskSignals|alerts/);
  });

  it("ignores empty known form filters and applies the selected scenario on the owner page", async () => {
    const OwnerPage = (await import("@/app/owner/page")).default;

    await OwnerPage({
      searchParams: Promise.resolve({
        employeeRef: "",
        scenario: "product_selection",
        salesStage: "",
        agentVersion: "",
      }),
    });

    expect(mocks.getOwnerReplyQualityView).toHaveBeenNthCalledWith(
      1,
      expect.anything(),
      mocks.directory,
      {
        tenantId: "tenant-secret",
        employeeUserId: undefined,
        scenario: "product_selection",
        salesStage: undefined,
        agentVersion: undefined,
      },
    );
  });

  it("renders tenant-level employee discovery, completion, reply use, results, and record links", async () => {
    const EmployeePage = (await import("@/app/owner/employees/[userId]/page")).default;
    const tree = await EmployeePage({
      params: Promise.resolve({ userId: "employee-view-1" }),
    });
    render(tree);

    expect(mocks.requireOwnerFromD1).toHaveBeenCalledWith(expect.any(Headers), mocks.d1);
    expect(mocks.getOwnerDashboard).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: "tenant-secret",
      employeeUserId: "employee-account-secret",
    }));
    expect(screen.getByRole("heading", { name: "小吴 · employee@shop.test" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "采用 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "完成 1/1（100%）" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "成交 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "超期 0" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "林女士 · C-0001" })).toHaveAttribute(
      "href",
      "/owner/customers/customer-view-1",
    );
  });

  it("renders an immutable safe customer timeline and owner-only vault links", async () => {
    const CustomerPage = (await import("@/app/owner/customers/[customerId]/page")).default;
    const tree = await CustomerPage({ params: Promise.resolve({ customerId: "customer-view-1" }) });
    const serialized = JSON.stringify(tree);
    render(tree);

    expect(mocks.requireOwnerFromD1).toHaveBeenCalledWith(expect.any(Headers), mocks.d1);
    expect(mocks.listTimeline).toHaveBeenCalledWith({
      tenantId: "tenant-secret",
      customerId: "customer-1",
    });
    expect(screen.getByRole("heading", { name: "林女士 · C-0001" })).toBeInTheDocument();
    expect(screen.getByText("客户询问保养方式")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /纠错|编辑|删除/ })).toBeNull();
    expect(screen.getByRole("link", { name: "查看商品图片" })).toHaveAttribute(
      "href",
      "/owner/vault/asset-1",
    );
    expect(serialized).not.toContain("vaultKey");
    expect(serialized).not.toContain("客户聊天原文");
  });

  it("authorizes before exposing only safe vault metadata to the client viewer", async () => {
    const VaultPage = (await import("@/app/owner/vault/[assetId]/page")).default;
    const tree = await VaultPage({ params: Promise.resolve({ assetId: "asset-1" }) });
    const serialized = JSON.stringify(tree);
    render(tree);

    expect(mocks.requireOwnerFromD1).toHaveBeenCalledWith(expect.any(Headers), mocks.d1);
    expect(mocks.getOwnerVaultAssetView).toHaveBeenCalledWith(
      mocks.d1,
      "tenant-secret",
      "asset-1",
      "2026-08-30T12:00:00.000Z",
    );
    expect(screen.getByRole("heading", { name: "原始资料保险库" })).toBeInTheDocument();
    expect(screen.getByText("原始资料将在 30 天后删除")).toBeInTheDocument();
    expect(serialized).not.toContain("vaultKey");
    expect(serialized).not.toContain("客户聊天原文");
  });
});
