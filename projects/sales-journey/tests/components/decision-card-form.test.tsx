import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DecisionCardForm } from "@/components/decision-card-form";
import { safeCard } from "../support/decision-card-fixtures";

const fetchMock = vi.fn();

function submittedBodies(): Array<{ evidence: unknown[] }> {
  return fetchMock.mock.calls.map(([, options]) =>
    JSON.parse((options as RequestInit).body as string) as { evidence: unknown[] },
  );
}

describe("DecisionCardForm 本地资料登记", () => {
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

  it("未确认资料不提交，确认后才提交 approved evidence", async () => {
    render(<DecisionCardForm />);

    fireEvent.change(screen.getByLabelText("商品描述"), { target: { value: "沉香身份珠" } });
    fireEvent.change(screen.getByLabelText("客户最新消息"), {
      target: { value: "身份珠有什么用？" },
    });
    fireEvent.change(screen.getByLabelText("资料标题"), {
      target: { value: "身份珠服务说明" },
    });

    fireEvent.click(screen.getByRole("button", { name: "生成决策卡" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(submittedBodies()[0]?.evidence).toEqual([]);

    fireEvent.click(screen.getByLabelText("本次选择引用服务端已批准的演示资料"));
    fireEvent.click(screen.getByRole("button", { name: "生成决策卡" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(submittedBodies()[1]?.evidence).toMatchObject([
      { status: "approved", title: "身份珠服务说明", verifiedFields: ["identity_bead"] },
    ]);
  });

  it("提交商品编号，让服务端快照替代手填商品事实", async () => {
    render(<DecisionCardForm />);

    fireEvent.change(screen.getByLabelText("商品编号（已同步时优先）"), {
      target: { value: "SYNTHETIC-001" },
    });
    fireEvent.change(screen.getByLabelText("客户最新消息"), {
      target: { value: "这件商品有什么特点？" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成决策卡" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(body.productCode).toBe("SYNTHETIC-001");
    expect(body.productDescription).toBeUndefined();
  });
  it("按业务大类选择具体场景，并只提交场景 ID", async () => {
    render(<DecisionCardForm />);

    fireEvent.change(screen.getByLabelText("业务大类"), {
      target: { value: "consignment_and_return" },
    });
    expect(screen.getByLabelText("具体场景")).toHaveValue("consignment_inquiry");

    fireEvent.change(screen.getByLabelText("具体场景"), {
      target: { value: "consignment_settlement" },
    });
    fireEvent.change(screen.getByLabelText("商品描述"), { target: { value: "绿松石" } });
    fireEvent.change(screen.getByLabelText("客户最新消息"), {
      target: { value: "想了解寄售结算流程" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成决策卡" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(body.scenario).toBe("consignment_settlement");
    expect(body).not.toHaveProperty("groupId");
    expect(body).not.toHaveProperty("tenantId");
    expect(body).not.toHaveProperty("operatorId");
  });

  it("可选择新增资料类型，且仍只提交人工确认后的资料", async () => {
    render(<DecisionCardForm />);

    fireEvent.change(screen.getByLabelText("商品描述"), { target: { value: "彩宝" } });
    fireEvent.change(screen.getByLabelText("客户最新消息"), { target: { value: "想了解活动规则" } });
    fireEvent.change(screen.getByLabelText("资料标题"), { target: { value: "本月活动说明" } });
    fireEvent.change(screen.getByLabelText("资料类型"), { target: { value: "price_or_payment" } });
    fireEvent.click(screen.getByLabelText("本次选择引用服务端已批准的演示资料"));
    fireEvent.click(screen.getByRole("button", { name: "生成决策卡" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(submittedBodies()[0]?.evidence).toMatchObject([
      { title: "本月活动说明", verifiedFields: ["price_or_payment"], status: "approved" },
    ]);
  });

  it.each([
    ["product_and_purchase", 11],
    ["order_and_delivery", 9],
    ["customization_and_adjustment", 8],
    ["defect_and_dispute", 8],
    ["after_sales_and_care", 7],
    ["identity_and_scan", 5],
    ["consignment_and_return", 9],
    ["follow_up_and_retention", 7],
  ])("显示 %s 的全部场景和人工复核兜底", (groupId, expectedOptions) => {
    render(<DecisionCardForm />);

    fireEvent.change(screen.getByLabelText("业务大类"), { target: { value: groupId } });

    const options = screen.getByLabelText("具体场景").querySelectorAll("option");
    expect(options).toHaveLength(expectedOptions);
    expect(options[options.length - 1]).toHaveTextContent("其他/不确定");
  });
});
