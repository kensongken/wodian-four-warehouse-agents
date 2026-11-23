import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DecisionCardView } from "@/components/decision-card-view";
import { ReplyFeedbackForm } from "@/components/reply-feedback-form";
import type { SalesJourneyContext } from "@/domain/sales-journey/schema";
import { safeCard } from "../support/decision-card-fixtures";

const fetchMock = vi.fn();
const demoCard = {
  ...safeCard,
  audit: {
    ...safeCard.audit,
    tenantId: "demo-tenant",
    operatorId: "demo-operator",
  },
};

const demoJourney: SalesJourneyContext = {
  tenantId: "demo-tenant",
  operatorId: "demo-operator",
  journeyId: "journey-demo-1",
  customerId: "customer-demo-1",
  subjectId: "subject-demo-1",
  currentStage: "evaluate",
  customerCategory: "复购客户",
  confirmedNeeds: [],
  pendingQuestions: [],
  commitments: [],
  openTasks: [],
  recentConversation: [],
};

const replacementCard = {
  ...demoCard,
  cardId: "card-replacement-2",
};

function acceptedResponse(
  privacyStatus: "safe" | "quarantined" = "safe",
  outcome: "created" | "updated" | "unchanged" = "created",
): Response {
  return new Response(JSON.stringify({
    accepted: true,
    outcome,
    privacyStatus,
    privacyReasonCodes: privacyStatus === "safe" ? [] : ["residual_address"],
  }), { status: outcome === "created" ? 201 : 200 });
}

function deferredResponse(): {
  promise: Promise<{ ok: boolean }>;
  resolve: (response: { ok: boolean }) => void;
} {
  let resolve: ((response: { ok: boolean }) => void) | undefined;
  const promise = new Promise<{ ok: boolean }>((nextResolve) => {
    resolve = nextResolve;
  });

  return {
    promise,
    resolve(response) {
      resolve?.(response);
    },
  };
}

describe("ReplyFeedbackForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.resetAllMocks();
  });

  it("submits helpful feedback with the server-owned demo identity", async () => {
    fetchMock.mockResolvedValue(acceptedResponse());

    render(<ReplyFeedbackForm card={demoCard} agentVersion="reply-agent.v1" />);
    fireEvent.click(screen.getByRole("button", { name: "👍 有帮助" }));

    expect(await screen.findByText("感谢评价，已进入优化池。")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/reply-feedback", expect.objectContaining({
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-tenant-id": "demo-tenant",
      },
      body: JSON.stringify({
        tenantId: "demo-tenant",
        operatorId: "demo-operator",
        cardId: "card-safe-1",
        agentVersion: "reply-agent.v1",
        rating: "helpful",
        reasons: [],
        scenario: "identity_bead",
        salesStage: "explain",
        evidenceIds: ["evidence-identity-1"],
      }),
    }));
  });

  it("requires an improvement reason before sending the final reply", () => {
    render(<ReplyFeedbackForm card={demoCard} agentVersion="reply-agent.v1" />);
    fireEvent.click(screen.getByRole("button", { name: "👎 需要改进" }));

    expect(screen.getByRole("button", { name: "提交评价" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("太长"));
    fireEvent.change(screen.getByLabelText("客服最后采用的回复"), {
      target: { value: "我会先核对资料，再为您说明。" },
    });

    expect(screen.getByRole("button", { name: "提交评价" })).toBeEnabled();
  });

  it("clears the optional final reply before submission while keeping automatic redaction in force", async () => {
    fetchMock.mockResolvedValue(acceptedResponse());

    render(<ReplyFeedbackForm card={demoCard} agentVersion="reply-agent.v1" />);
    fireEvent.click(screen.getByRole("button", { name: "👎 需要改进" }));
    fireEvent.click(screen.getByLabelText("太长"));
    fireEvent.change(screen.getByLabelText("客服最后采用的回复"), {
      target: { value: "请联系王小明，微信号 jade_sale88" },
    });

    expect(screen.getByText("提交前请删除姓名、电话、微信号、地址和支付信息；系统仍会自动脱敏。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清除最终回复" }));
    expect(screen.getByLabelText("客服最后采用的回复")).toHaveValue("");

    fireEvent.click(screen.getByRole("button", { name: "提交评价" }));
    await screen.findByText("感谢评价，已进入优化池。");
    const submittedPayload = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(submittedPayload).not.toHaveProperty("finalReply");
  });

  it("keeps an isolated final reply editable and lets the operator resubmit a safe replacement", async () => {
    fetchMock
      .mockResolvedValueOnce(acceptedResponse("quarantined"))
      .mockResolvedValueOnce(acceptedResponse("safe", "updated"));

    render(<ReplyFeedbackForm card={demoCard} agentVersion="reply-agent.v1" />);
    fireEvent.click(screen.getByRole("button", { name: "👎 需要改进" }));
    fireEvent.click(screen.getByLabelText("其他"));
    fireEvent.change(screen.getByLabelText("客服最后采用的回复"), {
      target: { value: "收货地址：幸福花园 3 栋 201 室" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交评价" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "评价已记录，但最终回复已隔离，不会进入优化池。请删除敏感内容后重新提交。",
    );
    expect(screen.getByLabelText("客服最后采用的回复")).toHaveValue(
      "收货地址：幸福花园 3 栋 201 室",
    );

    fireEvent.change(screen.getByLabelText("客服最后采用的回复"), {
      target: { value: "我会先核对资料，再为您说明。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "重新提交评价" }));

    expect(await screen.findByText("感谢评价，已进入优化池。")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps copy, voice, and journey write-back usable after a feedback failure", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    const onConfirmJourneyWriteBack = vi.fn();

    render(
      <>
        <DecisionCardView
          card={demoCard}
          journey={demoJourney}
          agentVersion="reply-agent.v1"
          onConfirmJourneyWriteBack={onConfirmJourneyWriteBack}
        />
        <ReplyFeedbackForm card={demoCard} agentVersion="reply-agent.v1" />
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "👍 有帮助" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "评价暂未提交，不影响您复制和跟进客户。",
      );
    });
    expect(screen.getByRole("button", { name: "👍 有帮助" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "复制回复" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "生成语音" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "确认本次旅程建议（本地演示）" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "重试提交" })).toBeEnabled();
  });

  it("clears a failed submission when the card identity changes so its retry cannot apply to the replacement", async () => {
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    const { rerender } = render(<ReplyFeedbackForm card={demoCard} agentVersion="reply-agent.v1" />);

    fireEvent.click(screen.getByRole("button", { name: "👎 需要改进" }));
    fireEvent.click(screen.getByLabelText("太长"));
    fireEvent.change(screen.getByLabelText("客服最后采用的回复"), {
      target: { value: "旧卡片的最终回复" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交评价" }));
    await screen.findByRole("alert");

    rerender(<ReplyFeedbackForm card={replacementCard} agentVersion="reply-agent.v2" />);

    expect(screen.queryByLabelText("客服最后采用的回复")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "重试提交" })).toBeNull();
    expect(screen.getByRole("button", { name: "👍 有帮助" })).toBeEnabled();
  });

  it("ignores a delayed feedback response after the card is replaced", async () => {
    const delayedResponse = deferredResponse();
    fetchMock.mockReturnValueOnce(delayedResponse.promise);
    const { rerender } = render(<ReplyFeedbackForm card={demoCard} agentVersion="reply-agent.v1" />);

    fireEvent.click(screen.getByRole("button", { name: "👍 有帮助" }));
    rerender(<ReplyFeedbackForm card={replacementCard} agentVersion="reply-agent.v2" />);

    await act(async () => {
      delayedResponse.resolve(acceptedResponse());
      await delayedResponse.promise;
    });

    expect(screen.queryByText("感谢评价，已进入优化池。")).toBeNull();
  });

  it("sends only one feedback request while pending and leaves reply actions enabled", () => {
    const delayedResponse = deferredResponse();
    fetchMock.mockReturnValue(delayedResponse.promise);

    render(
      <>
        <DecisionCardView
          card={demoCard}
          journey={demoJourney}
          agentVersion="reply-agent.v1"
          onConfirmJourneyWriteBack={vi.fn()}
        />
        <ReplyFeedbackForm card={demoCard} agentVersion="reply-agent.v1" />
      </>,
    );
    const helpfulButton = screen.getByRole("button", { name: "👍 有帮助" });
    fireEvent.click(helpfulButton);
    fireEvent.click(helpfulButton);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("正在提交评价…")).toBeInTheDocument();
    expect(helpfulButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "👎 需要改进" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "复制回复" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "生成语音" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "确认本次旅程建议（本地演示）" })).toBeEnabled();
  });
});
