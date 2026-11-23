import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CustomerCreateForm,
  FollowUpForm,
  type EmployeeDecisionSession,
} from "@/components/supervision/follow-up-form";
import type { EmployeeCustomerView } from "@/domain/supervision/employee-view";
import { safeCard } from "../support/decision-card-fixtures";

const customer: EmployeeCustomerView = {
  id: "customer-1",
  customerNumber: "C-0000000001",
  customerAlias: "林女士",
  category: "沉香",
  plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
  status: "pending",
};

const decision: EmployeeDecisionSession = {
  card: safeCard,
  usageToken: "signed-usage-token",
  usageTokenExpiresAt: "2999-08-30T08:15:00.000Z",
  cardInstanceId: "card-instance-1",
  cardDigest: `sha256_${"a".repeat(64)}`,
  generatedFor: {
    scenario: "identity_bead",
    productDescription: "身份珠",
    customerMessage: "想再确认身份珠的资料。",
  },
};

const fetchMock = vi.fn();

function fillRequiredFollowUp(): void {
  fireEvent.change(screen.getByLabelText("客户问题"), {
    target: { value: "想再确认身份珠的资料。" },
  });
  fireEvent.click(screen.getByLabelText("选择第 1 条回复"));
  fireEvent.click(screen.getByLabelText("已改写"));
  fireEvent.change(screen.getByLabelText("最终回复"), {
    target: { value: "我先帮您核对资料。" },
  });
  fireEvent.change(screen.getByLabelText("计划跟进时间"), {
    target: { value: "2026-08-31T14:30" },
  });
  fireEvent.change(screen.getByLabelText("实际跟进时间"), {
    target: { value: "2026-08-30T14:30" },
  });
  fireEvent.change(screen.getByLabelText("跟进结果"), {
    target: { value: "interested" },
  });
  fireEvent.change(screen.getByLabelText("下一步动作"), {
    target: { value: "明天下午确认到店时间" },
  });
}

describe("FollowUpForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("saves the selected reply, edit, schedule, result, and complete card binding after a 2xx", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ followUp: { id: "follow-up-1" } }), {
      status: 201,
      headers: { "content-type": "application/json" },
    }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);

    fillRequiredFollowUp();
    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));

    expect(screen.queryByText("已同步")).not.toBeInTheDocument();
    expect(await screen.findByText("已同步")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/supervision/customers/customer-1/follow-ups");
    expect(JSON.parse(String(options.body))).toMatchObject({
      usageToken: "signed-usage-token",
      cardInstanceId: "card-instance-1",
      cardDigest: `sha256_${"a".repeat(64)}`,
      decisionCardId: "card-safe-1",
      chosenReplyId: "reply-1",
      replyUsage: "edited",
      finalEditedText: "我先帮您核对资料。",
      result: "interested",
      nextAction: "明天下午确认到店时间",
    });
    expect(JSON.parse(String(options.body))).not.toHaveProperty("tenantId");
    expect(JSON.parse(String(options.body))).not.toHaveProperty("operatorId");
  });

  it("mounts optional reply evaluation under the active card with the adopted reply as its default text", () => {
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);

    fireEvent.click(screen.getByLabelText("选择第 1 条回复"));
    fireEvent.click(screen.getByLabelText("已采用"));
    fireEvent.click(screen.getByRole("button", { name: "👎 需要改进" }));

    expect(screen.getByRole("region", { name: "本次回复评价" })).toBeInTheDocument();
    expect(screen.getByLabelText("最终回复（可选）")).toHaveValue(safeCard.replyOptions[0]?.text);

    fireEvent.click(screen.getByLabelText("未使用"));
    expect(screen.getByLabelText("最终回复（可选）")).toHaveValue("");

    fireEvent.click(screen.getByLabelText("已改写"));
    fireEvent.change(screen.getByLabelText("最终回复"), { target: { value: "员工改写后的回复" } });
    expect(screen.getByLabelText("最终回复（可选）")).toHaveValue("员工改写后的回复");
  });

  it("keeps follow-up saving and copy available after optional feedback fails", async () => {
    fetchMock
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(new Response(JSON.stringify({ followUp: { id: "follow-up-1" } }), { status: 201 }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);

    fireEvent.click(screen.getByRole("button", { name: "👍 有帮助" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("不影响您复制和保存跟进");
    expect(screen.getByRole("button", { name: "复制回复" })).toBeEnabled();

    fillRequiredFollowUp();
    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));
    expect(await screen.findByText("已同步")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("preserves entered content and allows retry when sync fails", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ error: "failed" }), { status: 500 }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);

    fillRequiredFollowUp();
    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("尚未同步，请重试");
    expect(screen.getByLabelText("客户问题")).toHaveValue("想再确认身份珠的资料。");
    expect(screen.getByLabelText("最终回复")).toHaveValue("我先帮您核对资料。");
    expect(screen.getByRole("button", { name: "保存并同步给老板" })).toBeEnabled();
    expect(screen.queryByText("已同步")).not.toBeInTheDocument();
  });

  it("reuses the same follow-up request ID across a failed or ambiguous retry", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: "failed" }), { status: 500 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ followUp: { id: "follow-up-1" } }), {
        status: 201,
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ followUp: { id: "follow-up-2" } }), {
        status: 201,
      }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);
    fillRequiredFollowUp();

    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("尚未同步，请重试");
    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));
    expect(await screen.findByText("已同步")).toBeVisible();

    const requestIds = fetchMock.mock.calls.map(([, options]) =>
      JSON.parse(String((options as RequestInit).body)).requestId as string,
    );
    expect(requestIds).toHaveLength(2);
    expect(requestIds[1]).toBe(requestIds[0]);

    fireEvent.change(screen.getByLabelText("下一步动作"), {
      target: { value: "后天再次确认" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const nextOperationId = JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body)).requestId;
    expect(nextOperationId).not.toBe(requestIds[0]);
  });

  it("removes the synced badge when an employee changes saved content", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ followUp: { id: "follow-up-1" } }), {
      status: 201,
    }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);
    fillRequiredFollowUp();
    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));
    expect(await screen.findByText("已同步")).toBeVisible();

    fireEvent.change(screen.getByLabelText("下一步动作"), {
      target: { value: "改为后天联系" },
    });

    expect(screen.queryByText("已同步")).not.toBeInTheDocument();
  });

  it("prevents a second submission while the first request is still saving", async () => {
    let resolveRequest: ((response: Response) => void) | undefined;
    fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => {
      resolveRequest = resolve;
    }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);
    fillRequiredFollowUp();
    const button = screen.getByRole("button", { name: "保存并同步给老板" });

    fireEvent.click(button);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const savingButton = screen.getByRole("button", { name: "正在同步…" });
    expect(savingButton).toBeDisabled();
    fireEvent.click(savingButton);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolveRequest?.(new Response(JSON.stringify({ followUp: { id: "follow-up-1" } }), {
      status: 201,
    }));
    expect(await screen.findByText("已同步")).toBeVisible();
  });

  it("locks the confirmed follow-up operation so it cannot be submitted again unchanged", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ followUp: { id: "follow-up-1" } }), {
      status: 201,
    }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);
    fillRequiredFollowUp();

    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));
    expect(await screen.findByText("已同步")).toBeVisible();

    const savedButton = screen.getByRole("button", { name: "已保存" });
    expect(savedButton).toBeDisabled();
    fireEvent.click(savedButton);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("uploads selected product images to the private vault before saving the follow-up", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ assets: [{ id: "asset-1" }] }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ followUp: { id: "follow-up-1" } }), { status: 201 }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);
    fillRequiredFollowUp();
    const image = new File(["image"], "bracelet.jpg", { type: "image/jpeg" });
    fireEvent.change(screen.getByLabelText("客户商品图片（可选）"), {
      target: { files: [image] },
    });

    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));
    expect(await screen.findByText("已同步")).toBeVisible();

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/supervision/uploads");
    const uploadBody = fetchMock.mock.calls[0]?.[1]?.body;
    expect(uploadBody).toBeInstanceOf(FormData);
    expect((uploadBody as FormData).get("customerId")).toBe("customer-1");
    expect((uploadBody as FormData).get("files")).toBe(image);
  });

  it("generates a customer-bound decision without employee identity placeholders", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(decision), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" />);

    expect(screen.queryByLabelText(/租户|门店编号|操作员编号|tenant|operator/i)).toBeNull();
    fireEvent.change(screen.getByLabelText("商品描述"), { target: { value: "身份珠" } });
    fireEvent.change(screen.getByLabelText("客户问题"), {
      target: { value: "这个身份珠有什么用？" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成销售建议" }));

    expect(await screen.findByRole("heading", { name: "销售建议草稿" })).toBeInTheDocument();
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    const payload = JSON.parse(String(options.body));
    expect(payload.customerId).toBe("customer-1");
    expect(payload).not.toHaveProperty("tenantId");
    expect(payload).not.toHaveProperty("operatorId");
  });

  it("用商品编号生成销售建议，不要求手填商品事实", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(decision), { status: 200 }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" />);

    fireEvent.change(screen.getByLabelText("商品编号（已同步时优先）"), {
      target: { value: "SYNTHETIC-001" },
    });
    fireEvent.change(screen.getByLabelText("客户问题"), {
      target: { value: "这件商品有什么特点？" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成销售建议" }));

    expect(await screen.findByRole("heading", { name: "销售建议草稿" })).toBeInTheDocument();
    const payload = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(payload.productCode).toBe("SYNTHETIC-001");
    expect(payload.productDescription).toBeUndefined();
  });

  it("商品编号尚未同步时给出明确提示", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      error: "PRODUCT_STRATEGY_INPUT_NOT_FOUND",
    }), { status: 404 }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" />);

    fireEvent.change(screen.getByLabelText("商品编号（已同步时优先）"), {
      target: { value: "NOT-SYNCED-001" },
    });
    fireEvent.change(screen.getByLabelText("客户问题"), {
      target: { value: "这件商品有什么特点？" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成销售建议" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("该商品编号尚未同步");
  });

  it("ignores a delayed decision response after its generation inputs change", async () => {
    let resolveGeneration: ((response: Response) => void) | undefined;
    fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => {
      resolveGeneration = resolve;
    }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" />);
    fireEvent.change(screen.getByLabelText("商品描述"), { target: { value: "身份珠" } });
    fireEvent.change(screen.getByLabelText("客户问题"), {
      target: { value: "原来的客户问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成销售建议" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("客户问题"), {
      target: { value: "等待时改成的新问题" },
    });
    expect(screen.getByRole("button", { name: "生成销售建议" })).toBeEnabled();

    await act(async () => {
      resolveGeneration?.(new Response(JSON.stringify(decision), { status: 200 }));
    });

    expect(screen.queryByRole("heading", { name: "销售建议草稿" })).toBeNull();
    expect(screen.getByLabelText("客户问题")).toHaveValue("等待时改成的新问题");
    fireEvent.click(screen.getByLabelText("未使用"));
    fireEvent.change(screen.getByLabelText("下一步动作"), { target: { value: "重新生成后再跟进" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["业务场景", "silent_customer_followup"],
    ["商品描述", "改款后的身份珠"],
    ["客户问题", "我还想确认另一项资料。"],
  ])("invalidates a generated decision when %s changes and cannot save the stale session", (
    label,
    value,
  ) => {
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);
    expect(screen.getByRole("heading", { name: "销售建议草稿" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(label), { target: { value } });

    expect(screen.queryByRole("heading", { name: "销售建议草稿" })).toBeNull();
    expect(screen.getByRole("button", { name: "生成销售建议" })).toBeInTheDocument();
    expect(screen.getByLabelText(label)).toHaveValue(value);
    fireEvent.click(screen.getByLabelText("未使用"));
    fireEvent.change(screen.getByLabelText("下一步动作"), { target: { value: "重新核对" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("clears an expired decision session after the server rejects it and preserves the draft", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ error: "EXPIRED_USAGE_TOKEN" }), {
      status: 400,
      headers: { "content-type": "application/json" },
    }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);
    fillRequiredFollowUp();

    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("销售建议已过期，请重新生成");
    expect(screen.queryByRole("heading", { name: "销售建议草稿" })).toBeNull();
    expect(screen.getByRole("button", { name: "生成销售建议" })).toBeEnabled();
    expect(screen.getByLabelText("客户问题")).toHaveValue("想再确认身份珠的资料。");
    expect(screen.getByLabelText("最终回复")).toHaveValue("我先帮您核对资料。");
  });

  it("clears a stale product decision session and preserves the follow-up draft", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      error: "STALE_PRODUCT_STRATEGY",
    }), {
      status: 409,
      headers: { "content-type": "application/json" },
    }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);
    fillRequiredFollowUp();

    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("商品资料已更新，请重新生成决策卡后再保存跟进");
    expect(screen.queryByRole("heading", { name: "销售建议草稿" })).toBeNull();
    expect(screen.getByRole("button", { name: "生成销售建议" })).toBeEnabled();
    expect(screen.getByLabelText("客户问题")).toHaveValue("想再确认身份珠的资料。");
    expect(screen.getByLabelText("最终回复")).toHaveValue("我先帮您核对资料。");
  });

  it("keeps the decision session and draft when product freshness is temporarily unavailable", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      error: "PRODUCT_STRATEGY_RUNTIME_UNAVAILABLE",
    }), {
      status: 503,
      headers: { "content-type": "application/json" },
    }));
    render(<FollowUpForm customer={customer} employeeLabel="员工小吴" decision={decision} />);
    fillRequiredFollowUp();

    fireEvent.click(screen.getByRole("button", { name: "保存并同步给老板" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("尚未同步，请重试");
    expect(screen.getByRole("heading", { name: "销售建议草稿" })).toBeInTheDocument();
    expect(screen.getByLabelText("客户问题")).toHaveValue("想再确认身份珠的资料。");
    expect(screen.getByLabelText("最终回复")).toHaveValue("我先帮您核对资料。");
  });
});

describe("CustomerCreateForm", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("creates a customer from safe display data while marking the optional real name as vault-only", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ customer }), { status: 201 }));
    render(<CustomerCreateForm employeeLabel="员工小吴" />);

    fireEvent.change(screen.getByLabelText("客户称呼"), { target: { value: "林女士" } });
    fireEvent.change(screen.getByLabelText("真实姓名（仅存保险柜，可选）"), {
      target: { value: "林敏" },
    });
    fireEvent.change(screen.getByLabelText("计划跟进时间"), {
      target: { value: "2026-08-31T14:30" },
    });
    fireEvent.click(screen.getByRole("button", { name: "建立客户档案" }));

    expect(await screen.findByText("客户已建立")).toBeVisible();
    expect(screen.getByRole("link", { name: "进入客户跟进" })).toHaveAttribute(
      "href",
      "/employee/customers/customer-1",
    );
    expect(screen.queryByText("林敏")).not.toBeInTheDocument();
    const payload = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(payload).toMatchObject({ customerAlias: "林女士", realName: "林敏" });
    expect(payload).not.toHaveProperty("tenantId");
    expect(payload).not.toHaveProperty("operatorId");
  });

  it("reuses the customer request ID on failure and rotates it only after confirmed success", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: "failed" }), { status: 500 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ customer }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        customer: { ...customer, id: "customer-2" },
      }), { status: 201 }));
    render(<CustomerCreateForm employeeLabel="员工小吴" />);
    fireEvent.change(screen.getByLabelText("客户称呼"), { target: { value: "林女士" } });
    fireEvent.change(screen.getByLabelText("计划跟进时间"), {
      target: { value: "2026-08-31T14:30" },
    });

    fireEvent.click(screen.getByRole("button", { name: "建立客户档案" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("尚未同步，请重试");
    fireEvent.click(screen.getByRole("button", { name: "建立客户档案" }));
    expect(await screen.findByText("客户已建立")).toBeVisible();
    const firstTwoIds = fetchMock.mock.calls.slice(0, 2).map(([, options]) =>
      JSON.parse(String((options as RequestInit).body)).requestId as string,
    );
    expect(firstTwoIds[1]).toBe(firstTwoIds[0]);

    fireEvent.change(screen.getByLabelText("客户称呼"), { target: { value: "林姐" } });
    fireEvent.click(screen.getByRole("button", { name: "建立客户档案" }));
    expect(await screen.findByRole("link", { name: "进入客户跟进" })).toHaveAttribute(
      "href",
      "/employee/customers/customer-2",
    );
    const thirdId = JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body)).requestId as string;
    expect(thirdId).not.toBe(firstTwoIds[0]);
  });

  it("removes the saved status and link when a completed customer draft is edited", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ customer }), { status: 201 }));
    render(<CustomerCreateForm employeeLabel="员工小吴" />);
    fireEvent.change(screen.getByLabelText("客户称呼"), { target: { value: "林女士" } });
    fireEvent.change(screen.getByLabelText("计划跟进时间"), {
      target: { value: "2026-08-31T14:30" },
    });
    fireEvent.click(screen.getByRole("button", { name: "建立客户档案" }));
    expect(await screen.findByText("客户已建立")).toBeVisible();

    fireEvent.change(screen.getByLabelText("客户称呼"), { target: { value: "林姐" } });

    expect(screen.queryByText("客户已建立")).toBeNull();
    expect(screen.queryByText("已同步")).toBeNull();
    expect(screen.queryByRole("link", { name: "进入客户跟进" })).toBeNull();
    expect(screen.getByRole("button", { name: "建立客户档案" })).toBeEnabled();
  });

  it("locks every customer field while creation is in flight so the submitted draft stays truthful", async () => {
    let resolveRequest: ((response: Response) => void) | undefined;
    fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => {
      resolveRequest = resolve;
    }));
    render(<CustomerCreateForm employeeLabel="员工小吴" />);
    fireEvent.change(screen.getByLabelText("客户称呼"), { target: { value: "林女士" } });
    fireEvent.change(screen.getByLabelText("真实姓名（仅存保险柜，可选）"), {
      target: { value: "林敏" },
    });
    fireEvent.change(screen.getByLabelText("计划跟进时间"), {
      target: { value: "2026-08-31T14:30" },
    });
    fireEvent.click(screen.getByRole("button", { name: "建立客户档案" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    for (const label of [
      "客户称呼",
      "真实姓名（仅存保险柜，可选）",
      "珠宝品类",
      "计划跟进时间",
    ]) {
      expect(screen.getByLabelText(label)).toBeDisabled();
    }
    await act(async () => {
      resolveRequest?.(new Response(JSON.stringify({ customer }), { status: 201 }));
    });
    expect(await screen.findByText("客户已建立")).toBeVisible();
    expect(screen.getByLabelText("客户称呼")).toHaveValue("林女士");
    expect(screen.getByLabelText("真实姓名（仅存保险柜，可选）")).toHaveValue("");
  });
});
