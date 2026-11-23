import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CustomerTimeline } from "@/components/supervision/customer-timeline";
import type { EmployeeTimelineEntryView } from "@/domain/supervision/employee-view";

const timeline: EmployeeTimelineEntryView[] = [
  {
    kind: "follow_up",
    id: "follow-up-1",
    actorLabel: "员工小吴",
    customerMessageSummary: "品类：沉香；意图：了解身份珠。",
    plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
    actualFollowUpAt: "2026-08-30T06:30:00.000Z",
    result: "interested",
    nextAction: "明天确认到店",
    replyUsage: "edited",
    replySelection: "第 1 条回复",
    createdAt: "2026-08-30T06:31:00.000Z",
  },
  {
    kind: "decision_usage",
    id: "usage-1",
    actorLabel: "员工小吴",
    replyUsage: "edited",
    replySelection: "第 1 条回复",
    createdAt: "2026-08-30T06:31:00.000Z",
  },
  {
    kind: "correction",
    id: "correction-1",
    actorLabel: "员工小吴",
    followUpId: "follow-up-1",
    reason: "结果选错，补充说明。",
    createdAt: "2026-08-30T07:00:00.000Z",
  },
];

const fetchMock = vi.fn();

const secondFollowUp: EmployeeTimelineEntryView = {
  kind: "follow_up",
  id: "follow-up-2",
  actorLabel: "员工小吴",
  customerMessageSummary: "品类：翡翠；意图：确认到店时间。",
  plannedFollowUpAt: "2026-09-01T06:30:00.000Z",
  result: "pending",
  nextAction: "确认周五到店",
  replyUsage: "not_used",
  createdAt: "2026-08-30T09:30:00.000Z",
};

describe("CustomerTimeline", () => {
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("renders immutable safe events with actor, time, action, decision selection, and result", () => {
    render(
      <CustomerTimeline
        customerId="customer-1"
        entries={timeline}
      />,
    );

    expect(screen.getAllByText("员工小吴").length).toBeGreaterThan(0);
    expect(screen.getByText("客户跟进")).toBeInTheDocument();
    expect(screen.getByText("决策卡使用")).toBeInTheDocument();
    expect(screen.getByText("补充纠错")).toBeInTheDocument();
    expect(screen.getByText("品类：沉香；意图：了解身份珠。")).toBeInTheDocument();
    expect(screen.getByText("决策记录：第 1 条回复 · 已改写")).toBeInTheDocument();
    expect(screen.getByText(/意向中/)).toBeInTheDocument();
    expect(screen.getByText(/计划：/)).toBeInTheDocument();
    expect(screen.getByText(/实际：/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /编辑|删除/ })).toBeNull();
  });

  it("appends a correction linked to the original follow-up without edit or delete controls", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      correction: {
        kind: "correction",
        id: "correction-2",
        followUpId: "follow-up-1",
        actorLabel: "员工小吴",
        reason: "补充：客户希望下周再联系。",
        createdAt: "2026-08-30T08:00:00.000Z",
      },
    }), { status: 201 }));
    render(
      <CustomerTimeline
        customerId="customer-1"
        entries={timeline}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "为本条添加纠错" }));
    fireEvent.change(screen.getByLabelText("纠错说明"), {
      target: { value: "补充：客户希望下周再联系。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交补充纠错" }));

    expect(await screen.findByText("纠错已追加")).toBeVisible();
    expect(screen.getByText("补充：客户希望下周再联系。")).toBeInTheDocument();
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/supervision/customers/customer-1/corrections");
    expect(JSON.parse(String(options.body))).toMatchObject({
      followUpId: "follow-up-1",
      reason: "补充：客户希望下周再联系。",
    });
    expect(screen.queryByRole("button", { name: /编辑|删除/ })).toBeNull();
  });

  it("reuses one correction request ID after a lost response and rotates for a new correction", async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError("response lost"))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        correction: {
          kind: "correction",
          id: "correction-2",
          followUpId: "follow-up-1",
          actorLabel: "员工小吴",
          reason: "补充：客户希望下周联系。",
          createdAt: "2026-08-30T08:00:00.000Z",
        },
      }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        correction: {
          kind: "correction",
          id: "correction-3",
          followUpId: "follow-up-1",
          actorLabel: "员工小吴",
          reason: "再次补充：改到周五。",
          createdAt: "2026-08-30T09:00:00.000Z",
        },
      }), { status: 201 }));
    render(
      <CustomerTimeline
        customerId="customer-1"
        entries={timeline}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "为本条添加纠错" }));
    fireEvent.change(screen.getByLabelText("纠错说明"), {
      target: { value: "补充：客户希望下周联系。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交补充纠错" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("尚未同步，请重试");
    fireEvent.click(screen.getByRole("button", { name: "提交补充纠错" }));
    expect(await screen.findByText("纠错已追加")).toBeVisible();

    const firstTwoIds = fetchMock.mock.calls.slice(0, 2).map(([, options]) =>
      JSON.parse(String((options as RequestInit).body)).requestId as string,
    );
    expect(firstTwoIds[1]).toBe(firstTwoIds[0]);

    fireEvent.click(screen.getByRole("button", { name: "为本条添加纠错" }));
    fireEvent.change(screen.getByLabelText("纠错说明"), {
      target: { value: "再次补充：改到周五。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交补充纠错" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(await screen.findByText("再次补充：改到周五。")).toBeInTheDocument();
    const thirdId = JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body)).requestId as string;
    expect(thirdId).not.toBe(firstTwoIds[0]);
  });

  it("locks every correction control while a delayed save is in flight", async () => {
    let resolveRequest: ((response: Response) => void) | undefined;
    fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => {
      resolveRequest = resolve;
    }));
    render(
      <CustomerTimeline
        customerId="customer-1"
        entries={[timeline[0]!, secondFollowUp, ...timeline.slice(1)]}
      />,
    );

    const targetButtons = screen.getAllByRole("button", { name: "为本条添加纠错" });
    fireEvent.click(targetButtons[0]!);
    fireEvent.change(screen.getByLabelText("纠错说明"), {
      target: { value: "这条说明正在保存。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交补充纠错" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    for (const button of targetButtons) expect(button).toBeDisabled();
    expect(screen.getByLabelText("纠错说明")).toBeDisabled();
    expect(screen.getByRole("button", { name: "正在提交…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();

    fireEvent.click(targetButtons[1]!);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByLabelText("纠错说明")).toHaveValue("这条说明正在保存。");

    await act(async () => {
      resolveRequest?.(new Response(JSON.stringify({
        correction: {
          kind: "correction",
          id: "correction-delayed",
          followUpId: "follow-up-1",
          actorLabel: "员工小吴",
          reason: "这条说明正在保存。",
          createdAt: "2026-08-30T10:00:00.000Z",
        },
      }), { status: 201 }));
    });
    expect(await screen.findByText("这条说明正在保存。")).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "为本条添加纠错" })[1]!);
    fireEvent.change(screen.getByLabelText("纠错说明"), {
      target: { value: "延迟响应完成后的新草稿。" },
    });
    expect(screen.getByLabelText("纠错说明")).toHaveValue("延迟响应完成后的新草稿。");
  });

  it("rotates the correction request ID when a failed draft is changed before retry", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: "failed" }), { status: 500 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        correction: {
          kind: "correction",
          id: "correction-changed",
          followUpId: "follow-up-1",
          actorLabel: "员工小吴",
          reason: "失败后改过的说明。",
          createdAt: "2026-08-30T10:30:00.000Z",
        },
      }), { status: 201 }));
    render(<CustomerTimeline customerId="customer-1" entries={timeline} />);
    fireEvent.click(screen.getByRole("button", { name: "为本条添加纠错" }));
    fireEvent.change(screen.getByLabelText("纠错说明"), {
      target: { value: "第一次提交的说明。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交补充纠错" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("尚未同步，请重试");

    fireEvent.change(screen.getByLabelText("纠错说明"), {
      target: { value: "失败后改过的说明。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交补充纠错" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("失败后改过的说明。")).toBeInTheDocument();

    const requests = fetchMock.mock.calls.map(([, options]) =>
      JSON.parse(String((options as RequestInit).body)) as {
        requestId: string;
        reason: string;
      },
    );
    expect(requests[0]?.reason).toBe("第一次提交的说明。");
    expect(requests[1]?.reason).toBe("失败后改过的说明。");
    expect(requests[1]?.requestId).not.toBe(requests[0]?.requestId);
  });
});
