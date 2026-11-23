import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { DecisionCardForm } from "@/components/decision-card-form";
import { DecisionCardView } from "@/components/decision-card-view";
import type { SalesJourneyContext } from "@/domain/sales-journey/schema";
import { safeCard } from "../support/decision-card-fixtures";

const fetchMock = vi.fn();

const journeyContext = {
  tenantId: "demo-tenant",
  journeyId: "journey-demo-1",
  customerId: "customer-demo-1",
  subjectId: "subject-demo-1",
  operatorId: "demo-operator",
  currentStage: "evaluate" as const,
  customerCategory: "复购客户",
  confirmedNeeds: ["想比较佩戴效果"],
  pendingQuestions: ["偏好的珠径"],
  commitments: ["今天发送对比建议"],
  openTasks: ["核对现货"],
  recentConversation: [{ speaker: "customer" as const, text: "两款佩戴起来有什么区别？" }],
};

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify({ card: safeCard }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it("shows journey context and does not write back before confirmation", async () => {
  const onConfirmJourneyWriteBack = vi.fn();

  render(
    <DecisionCardView
      card={safeCard}
      journey={journeyContext}
      agentVersion="reply-agent.v1"
      onConfirmJourneyWriteBack={onConfirmJourneyWriteBack}
    />,
  );

  expect(screen.getByText("当前阶段：对比评估")).toBeInTheDocument();
  expect(screen.getByText("想比较佩戴效果")).toBeInTheDocument();
  expect(onConfirmJourneyWriteBack).not.toHaveBeenCalled();

  expect(screen.getByText("建议阶段：说明方案")).toBeInTheDocument();
  expect(screen.getByText(`建议下一步：${safeCard.nextAction.text}`)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "确认本次旅程建议（本地演示）" }));

  await waitFor(() => {
    expect(onConfirmJourneyWriteBack).toHaveBeenCalledWith(expect.objectContaining({
      confirmedByOperator: true,
      proposedStage: safeCard.salesStage,
    }));
  });
  expect(await screen.findByText("本页确认完成，未写入真实系统。")).toBeInTheDocument();
});

it("runtime-validates the proposed write-back before invoking the local confirmation", () => {
  const onConfirmJourneyWriteBack = vi.fn();
  const invalidJourney = { ...journeyContext, tenantId: " " } as SalesJourneyContext;

  render(
    <DecisionCardView
      card={safeCard}
      journey={invalidJourney}
      agentVersion="reply-agent.v1"
      onConfirmJourneyWriteBack={onConfirmJourneyWriteBack}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "确认本次旅程建议（本地演示）" }));

  expect(screen.getByRole("alert")).toHaveTextContent("旅程建议无效");
  expect(onConfirmJourneyWriteBack).not.toHaveBeenCalled();
});

it("keeps a failed local confirmation retryable without claiming a durable write-back", async () => {
  const onConfirmJourneyWriteBack = vi.fn()
    .mockRejectedValueOnce(new Error("local failure"))
    .mockResolvedValueOnce(undefined);

  render(
    <DecisionCardView
      card={safeCard}
      journey={journeyContext}
      agentVersion="reply-agent.v1"
      onConfirmJourneyWriteBack={onConfirmJourneyWriteBack}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "确认本次旅程建议（本地演示）" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("本地确认失败");
  expect(screen.queryByText(/已回写/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试本地确认" }));
  await waitFor(() => expect(onConfirmJourneyWriteBack).toHaveBeenCalledTimes(2));
  expect(await screen.findByText("本页确认完成，未写入真实系统。")).toBeInTheDocument();
});

it("requires a new confirmation after a replacement decision card is generated", async () => {
  fetchMock.mockReset();
  fetchMock
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ card: safeCard }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    )
    .mockResolvedValueOnce(
      new Response(JSON.stringify({
        card: { ...safeCard, cardId: "card-safe-2", summary: "新的销售建议" },
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

  render(<DecisionCardForm journey={journeyContext} />);

  fireEvent.change(screen.getByLabelText("商品描述"), { target: { value: "沉香身份珠" } });
  fireEvent.change(screen.getByLabelText("客户最新消息"), {
    target: { value: "身份珠有什么用？" },
  });
  fireEvent.click(screen.getByRole("button", { name: "生成决策卡" }));
  await screen.findByRole("button", { name: "确认本次旅程建议（本地演示）" });

  fireEvent.click(screen.getByRole("button", { name: "确认本次旅程建议（本地演示）" }));
  expect(screen.getByText("已确认本次旅程建议（本地演示）")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "生成决策卡" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  await screen.findByText("新的销售建议");

  expect(screen.queryByText("已确认本次旅程建议（本地演示）")).not.toBeInTheDocument();
});
