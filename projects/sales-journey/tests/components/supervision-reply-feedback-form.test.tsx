import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SupervisionReplyFeedbackForm } from "@/components/reply-feedback-form";

const fetchMock = vi.fn();

function acceptedResponse(privacyStatus: "safe" | "quarantined" = "safe"): Response {
  return new Response(JSON.stringify({
    accepted: true,
    outcome: "created",
    privacyStatus,
    privacyReasonCodes: privacyStatus === "safe" ? [] : ["residual_address"],
  }), { status: 201 });
}

function feedbackForm(overrides: Partial<React.ComponentProps<typeof SupervisionReplyFeedbackForm>> = {}) {
  return (
    <SupervisionReplyFeedbackForm
      key={overrides.cardInstanceId ?? "card-instance-1"}
      customerId="customer-1"
      usageToken="signed-usage-token"
      cardInstanceId="card-instance-1"
      {...overrides}
    />
  );
}

describe("SupervisionReplyFeedbackForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.resetAllMocks();
  });

  it("submits helpful feedback without a final reply", async () => {
    fetchMock.mockResolvedValue(acceptedResponse());
    render(feedbackForm({ defaultFinalReply: "已采用的回复不应随好评提交" }));

    fireEvent.click(screen.getByRole("button", { name: "👍 有帮助" }));

    expect(await screen.findByText("感谢评价，已进入优化池。")).toBeVisible();
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      usageToken: "signed-usage-token",
      rating: "helpful",
      reasonCodes: [],
    });
  });

  it("requires a reason before sending needs-improvement feedback", () => {
    render(feedbackForm());
    fireEvent.click(screen.getByRole("button", { name: "👎 需要改进" }));

    expect(screen.getByRole("button", { name: "提交评价" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("太长"));
    expect(screen.getByRole("button", { name: "提交评价" })).toBeEnabled();
  });

  it("submits the edited final reply", async () => {
    fetchMock.mockResolvedValue(acceptedResponse());
    render(feedbackForm({ defaultFinalReply: "我先帮您核对资料。" }));
    fireEvent.click(screen.getByRole("button", { name: "👎 需要改进" }));
    fireEvent.click(screen.getByLabelText("太长"));
    fireEvent.click(screen.getByRole("button", { name: "提交评价" }));

    await screen.findByRole("status");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toMatchObject({
      rating: "needs_improvement",
      reasonCodes: ["too_long"],
      finalReply: "我先帮您核对资料。",
    });
  });

  it("states that quarantined reply text was not saved", async () => {
    fetchMock.mockResolvedValue(acceptedResponse("quarantined"));
    render(feedbackForm({ defaultFinalReply: "收货地址：幸福花园 3 栋 201 室" }));
    fireEvent.click(screen.getByRole("button", { name: "👎 需要改进" }));
    fireEvent.click(screen.getByLabelText("其他"));
    fireEvent.click(screen.getByRole("button", { name: "提交评价" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("回复文本没有保存");
  });

  it("lets an employee clear or edit quarantined text and resubmit safely", async () => {
    fetchMock.mockResolvedValueOnce(acceptedResponse("quarantined")).mockResolvedValueOnce(acceptedResponse());
    render(feedbackForm({ defaultFinalReply: "收货地址：幸福花园 3 栋 201 室" }));
    fireEvent.click(screen.getByRole("button", { name: "👎 需要改进" }));
    fireEvent.click(screen.getByLabelText("其他"));
    fireEvent.click(screen.getByRole("button", { name: "提交评价" }));
    await screen.findByRole("alert");

    fireEvent.click(screen.getByRole("button", { name: "清除回复文本" }));
    expect(screen.getByLabelText("最终回复（可选）")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("最终回复（可选）"), {
      target: { value: "我会先核对资料，再为您说明。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "重新提交评价" }));

    expect(await screen.findByText("感谢评价，已进入优化池。")).toBeVisible();
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toMatchObject({
      finalReply: "我会先核对资料，再为您说明。",
    });
  });

  it("sends only one feedback request while pending", () => {
    fetchMock.mockReturnValue(new Promise<Response>(() => {}));
    render(feedbackForm());
    const helpful = screen.getByRole("button", { name: "👍 有帮助" });

    fireEvent.click(helpful);
    fireEvent.click(helpful);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(helpful).toBeDisabled();
  });

  it("clears a failed retry when a new card instance replaces the old card", async () => {
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    const { rerender } = render(feedbackForm());
    fireEvent.click(screen.getByRole("button", { name: "👍 有帮助" }));
    await screen.findByRole("alert");

    rerender(feedbackForm({ cardInstanceId: "card-instance-2" }));

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "重试提交" })).toBeNull();
    expect(screen.getByRole("button", { name: "👍 有帮助" })).toBeEnabled();
  });

  it("does not block sibling copy or follow-up save controls when feedback fails", async () => {
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    render(
      <>
        <button type="button">复制回复</button>
        <button type="button">保存并同步给老板</button>
        {feedbackForm()}
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "👍 有帮助" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("不影响您复制和保存跟进"));
    expect(screen.getByRole("button", { name: "复制回复" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "保存并同步给老板" })).toBeEnabled();
  });
});
