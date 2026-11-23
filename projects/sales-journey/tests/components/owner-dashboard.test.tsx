import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  OwnerDashboard,
  OwnerEmployeeDetail,
} from "@/components/supervision/owner-dashboard";
import type { OwnerDashboardBrowserView } from "@/server/supervision/owner-view";
import type { OwnerReplyQualityView } from "@/domain/supervision/contracts";

const RECORD_REFS = Array.from({ length: 9 }, (_, index) => `row-${index + 1}`);

const viewFixture: OwnerDashboardBrowserView = {
  generatedAt: "2026-08-30T12:00:00.000Z",
  summaryMetrics: [
    { key: "todaySubmitted", label: "今日提交", valueLabel: "9", recordRefs: RECORD_REFS },
    { key: "pending", label: "待跟进", valueLabel: "9", recordRefs: RECORD_REFS },
    { key: "overdue", label: "已超期", valueLabel: "1", recordRefs: ["row-1"] },
    { key: "won", label: "已成交", valueLabel: "0", recordRefs: [] },
  ],
  employees: [
    {
      navigationId: "employee-view-a",
      displayLabel: "小林 · lin@shop.test",
      metrics: [
        { key: "submitted", label: "提交", valueLabel: "8", recordRefs: RECORD_REFS.slice(0, 8) },
        { key: "completionRate", label: "完成", valueLabel: "2/8（25%）", recordRefs: ["row-1", "row-2"] },
        { key: "overdue", label: "超期", valueLabel: "1", recordRefs: ["row-1"] },
        { key: "adopted", label: "采用", valueLabel: "1", recordRefs: ["row-1"] },
        { key: "edited", label: "改写", valueLabel: "1", recordRefs: ["row-2"] },
        { key: "notUsed", label: "未用", valueLabel: "6", recordRefs: RECORD_REFS.slice(2, 8) },
        { key: "won", label: "成交", valueLabel: "1", recordRefs: ["row-1"] },
        { key: "interested", label: "意向", valueLabel: "1", recordRefs: ["row-2"] },
        { key: "lost", label: "未成交", valueLabel: "1", recordRefs: ["row-3"] },
      ],
    },
    {
      navigationId: "employee-view-b",
      displayLabel: "小吴 · wu@shop.test",
      metrics: [
        { key: "submitted", label: "提交", valueLabel: "1", recordRefs: ["row-9"] },
        { key: "completionRate", label: "完成", valueLabel: "1/1（100%）", recordRefs: ["row-9"] },
        { key: "overdue", label: "超期", valueLabel: "1", recordRefs: ["row-9"] },
        { key: "adopted", label: "采用", valueLabel: "0", recordRefs: [] },
        { key: "edited", label: "改写", valueLabel: "0", recordRefs: [] },
        { key: "notUsed", label: "未用", valueLabel: "1", recordRefs: ["row-9"] },
        { key: "won", label: "成交", valueLabel: "0", recordRefs: [] },
        { key: "interested", label: "意向", valueLabel: "0", recordRefs: [] },
        { key: "lost", label: "未成交", valueLabel: "0", recordRefs: [] },
      ],
    },
    {
      navigationId: "employee-view-empty",
      displayLabel: "小周 · zhou@shop.test",
      metrics: [
        { key: "submitted", label: "提交", valueLabel: "0", recordRefs: [] },
        { key: "completionRate", label: "完成", valueLabel: "0/0（0%）", recordRefs: [] },
        { key: "overdue", label: "超期", valueLabel: "0", recordRefs: [] },
        { key: "adopted", label: "采用", valueLabel: "0", recordRefs: [] },
        { key: "edited", label: "改写", valueLabel: "0", recordRefs: [] },
        { key: "notUsed", label: "未用", valueLabel: "0", recordRefs: [] },
        { key: "won", label: "成交", valueLabel: "0", recordRefs: [] },
        { key: "interested", label: "意向", valueLabel: "0", recordRefs: [] },
        { key: "lost", label: "未成交", valueLabel: "0", recordRefs: [] },
      ],
    },
  ],
  records: RECORD_REFS.map((ref, index) => ({
    ref,
    customerNavigationId: `customer-view-${index + 1}`,
    customerNumber: `C-${String(index + 1).padStart(4, "0")}`,
    customerAlias: `客户 ${index + 1}`,
    employeeNavigationId: index === 8 ? "employee-view-b" : "employee-view-a",
    employeeLabel: index === 8 ? "小吴 · wu@shop.test" : "小林 · lin@shop.test",
    category: "沉香",
    submittedAt: "2026-08-30T08:00:00.000Z",
    plannedFollowUpAt: "2026-08-30T09:00:00.000Z",
    decisionUsage: "not_used",
    nextAction: "人工跟进",
    result: "pending",
    riskLabels: index === 0 ? ["overdue_follow_up"] : [],
  })),
  customers: RECORD_REFS.map((_, index) => ({
    navigationId: `customer-view-${index + 1}`,
    customerNumber: `C-${String(index + 1).padStart(4, "0")}`,
    customerAlias: `客户 ${index + 1}`,
    category: "沉香",
    status: "pending",
  })),
  risks: [{
    kind: "overdue_follow_up",
    label: "跟进超期",
    recordRefs: ["row-1"],
  }],
};

const replyQualityFixture: OwnerReplyQualityView = {
  totals: { helpful: 1, needsImprovement: 1, quarantined: 1 },
  reasons: [
    { code: "too_long", count: 1 },
    { code: "wrong_stage", count: 1 },
  ],
  samples: [
    {
      feedbackRef: "feedback-row-1",
      employeeRef: "employee-view-a",
      customerRef: "customer-view-1",
      scenario: "product_selection",
      salesStage: "evaluate",
      agentVersion: "reply-agent.v2",
      rating: "helpful",
      privacyStatus: "safe",
      sanitizedFinalReply: "SAFE_SANITIZED_REPLY",
      privacyReasonCodes: [],
    },
    {
      feedbackRef: "feedback-row-2",
      employeeRef: "employee-view-b",
      customerRef: "customer-view-9",
      scenario: "identity_bead",
      salesStage: "explain",
      agentVersion: "reply-agent.v3",
      rating: "needs_improvement",
      privacyStatus: "quarantined",
      privacyReasonCodes: ["residual_phone"],
    },
  ],
};

function detailRefs(): Array<string | null> {
  return screen.queryAllByTestId("detail-row").map((row) => row.getAttribute("data-record-ref"));
}

describe("OwnerDashboard", () => {
  it("drills summary metrics to the exact safe row references", () => {
    render(<OwnerDashboard view={viewFixture} replyQuality={replyQualityFixture} />);
    fireEvent.click(screen.getByRole("button", { name: "待跟进 9" }));
    expect(detailRefs()).toEqual(RECORD_REFS);
  });

  it("drills every employee-row figure to its exact safe row set", () => {
    render(<OwnerDashboard view={viewFixture} replyQuality={replyQualityFixture} />);
    fireEvent.click(screen.getByRole("button", { name: "小林 · lin@shop.test：提交 8" }));
    expect(detailRefs()).toEqual(RECORD_REFS.slice(0, 8));
    fireEvent.click(screen.getByRole("button", { name: "小林 · lin@shop.test：完成 2/8（25%）" }));
    expect(detailRefs()).toEqual(["row-1", "row-2"]);
    fireEvent.click(screen.getByRole("button", { name: "小林 · lin@shop.test：超期 1" }));
    expect(detailRefs()).toEqual(["row-1"]);
  });

  it("drills each risk count to the same rows counted by the signal", () => {
    render(<OwnerDashboard view={viewFixture} replyQuality={replyQualityFixture} />);
    fireEvent.click(screen.getByRole("button", { name: "跟进超期 1" }));
    expect(detailRefs()).toEqual(["row-1"]);
  });

  it("drills a zero-followup duplicate signal to safe customer rows", () => {
    render(<OwnerDashboard view={{ ...viewFixture, risks: [{ kind: "duplicate_customer", label: "疑似重复客户", recordRefs: ["customer-row-customer-view-1"] }] }} replyQuality={replyQualityFixture} />);
    fireEvent.click(screen.getByRole("button", { name: "疑似重复客户 1" }));
    expect(detailRefs()).toEqual(["customer-row-customer-view-1"]);
  });

  it("puts execution and employee discovery before business results without scores", () => {
    const { container } = render(<OwnerDashboard view={viewFixture} replyQuality={replyQualityFixture} />);
    const execution = screen.getByRole("heading", { name: "今日执行" });
    const employees = screen.getByRole("heading", { name: "员工执行" });
    const results = screen.getByRole("heading", { name: "业务结果" });
    expect(execution.compareDocumentPosition(employees) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(employees.compareDocumentPosition(results) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("link", { name: "查看小周 · zhou@shop.test详情" })).toHaveAttribute(
      "href",
      "/owner/employees/employee-view-empty",
    );
    expect(container).not.toHaveTextContent(/绩效评分|员工得分|排名/);
  });

  it("uses identical partial alias and partial-number semantics in screen rows and export URL", () => {
    render(<OwnerDashboard view={viewFixture} replyQuality={replyQualityFixture} />);
    const resultTable = screen.getByRole("table", { name: "业务结果明细" });
    const filter = screen.getByLabelText("客户编号或称呼");
    fireEvent.change(filter, { target: { value: "客户 9" } });
    expect(within(resultTable).getAllByRole("row")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "导出当前筛选" })).toHaveAttribute(
      "href",
      "/api/supervision/owner/export?customerQuery=%E5%AE%A2%E6%88%B7+9",
    );
    fireEvent.change(filter, { target: { value: "0009" } });
    expect(within(resultTable).getAllByRole("row")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "导出当前筛选" })).toHaveAttribute(
      "href",
      "/api/supervision/owner/export?customerQuery=0009",
    );
  });

  it("显示回复质量而不给员工排名，隔离样本只显示元数据", () => {
    const { container } = render(
      <OwnerDashboard view={viewFixture} replyQuality={replyQualityFixture} />,
    );
    const heading = screen.getByRole("heading", { name: "回复质量（不用于员工打分）" });
    const panel = heading.closest("section");
    expect(panel).not.toBeNull();
    expect(within(panel!).getByText("SAFE_SANITIZED_REPLY")).toBeVisible();
    expect(within(panel!).getByText("小林 · lin@shop.test", { selector: "strong" })).toBeVisible();
    expect(within(panel!).getByText("客户 1 · C-0001")).toBeVisible();
    expect(within(panel!).getByText(/residual_phone/u)).toBeVisible();
    expect(container).not.toHaveTextContent(/employee-a|customer-a|RAW_QUARANTINED_REPLY/u);
    expect(container).not.toHaveTextContent(/绩效评分|员工得分|排名/u);
  });

  it("提交安全的员工、场景、阶段和版本筛选", () => {
    const { rerender } = render(
      <OwnerDashboard view={viewFixture} replyQuality={replyQualityFixture} />,
    );
    const panel = screen.getByRole("heading", {
      name: "回复质量（不用于员工打分）",
    }).closest("section")!;
    const form = within(panel).getByRole("button", { name: "应用质量筛选" }).closest("form")!;
    expect(form).toHaveAttribute("action", "/owner");
    expect(within(panel).getByLabelText("质量负责人")).toHaveAttribute("name", "employeeRef");
    expect(within(panel).getByLabelText("销售场景")).toHaveAttribute("name", "scenario");
    expect(within(panel).getByLabelText("销售阶段")).toHaveAttribute("name", "salesStage");
    expect(within(panel).getByLabelText("Agent 版本")).toHaveAttribute("name", "agentVersion");

    rerender(
      <OwnerDashboard
        view={viewFixture}
        replyQuality={{
          totals: { helpful: 0, needsImprovement: 1, quarantined: 1 },
          reasons: [{ code: "too_long", count: 1 }],
          samples: [replyQualityFixture.samples[1]!],
        }}
        replyQualityFilters={{
          employeeRef: "employee-view-b",
          scenario: "identity_bead",
          salesStage: "explain",
          agentVersion: "reply-agent.v3",
        }}
        replyQualityAgentVersions={["reply-agent.v2", "reply-agent.v3"]}
      />,
    );
    const filteredPanel = screen.getByRole("heading", {
      name: "回复质量（不用于员工打分）",
    }).closest("section")!;
    expect(within(filteredPanel).getByLabelText("质量负责人")).toHaveValue("employee-view-b");
    expect(within(filteredPanel).getByLabelText("销售场景")).toHaveValue("identity_bead");
    expect(within(filteredPanel).getByLabelText("销售阶段")).toHaveValue("explain");
    expect(within(filteredPanel).getByLabelText("Agent 版本")).toHaveValue("reply-agent.v3");
    expect(within(filteredPanel).queryByText("SAFE_SANITIZED_REPLY")).toBeNull();
    expect(within(filteredPanel).getByText("有帮助 0")).toBeVisible();
    expect(within(filteredPanel).getByText("需要改进 1")).toBeVisible();
    expect(within(filteredPanel).getByText("隔离样本 1")).toBeVisible();
  });
});

describe("OwnerEmployeeDetail", () => {
  it("drills reply-use and result figures to exact safe row references", () => {
    render(
      <OwnerEmployeeDetail
        employee={viewFixture.employees[0]!}
        records={viewFixture.records.slice(0, 8)}
        customers={viewFixture.customers.slice(0, 8)}
      />,
    );
    const expected: ReadonlyArray<readonly [string, readonly string[]]> = [
      ["完成 2/8（25%）", ["row-1", "row-2"]],
      ["采用 1", ["row-1"]],
      ["改写 1", ["row-2"]],
      ["未用 6", RECORD_REFS.slice(2, 8)],
      ["成交 1", ["row-1"]],
      ["意向 1", ["row-2"]],
      ["未成交 1", ["row-3"]],
      ["超期 1", ["row-1"]],
    ];
    for (const [name, refs] of expected) {
      fireEvent.click(screen.getByRole("button", { name }));
      expect(detailRefs()).toEqual(refs);
    }
  });
});
