import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DecisionCardView } from "@/components/decision-card-view";
import { createDecisionCard } from "@/domain/decision-card/advisor";
import {
  controlledReuseOrReturnRequest,
  identityBeadRequest,
  safeCard,
} from "../support/decision-card-fixtures";

const fetchMock = vi.fn();
const createObjectURLMock = vi.fn();
const revokeObjectURLMock = vi.fn();

function mockVoiceResponse(status: number, body: Blob | { error: string }): void {
  fetchMock.mockResolvedValueOnce(
    new Response(body instanceof Blob ? body : JSON.stringify(body), {
      status,
      headers:
        body instanceof Blob
          ? { "x-voice-downloadable": "true" }
          : { "content-type": "application/json" },
    }),
  );
}

describe("DecisionCardView", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      createObjectURL: createObjectURLMock,
      revokeObjectURL: revokeObjectURLMock,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("只展示销售人员可读的建议，不展示内部意图或阶段代码", () => {
    render(<DecisionCardView card={createDecisionCard(identityBeadRequest)} />);

    expect(screen.getByRole("heading", { name: "销售建议草稿" })).toBeInTheDocument();
    expect(screen.getByText("依据已批准资料说明身份珠的核对方式。")).toBeInTheDocument();
    expect(screen.queryByText(/客户意图：|销售阶段：|learn_identity|explain/)).not.toBeInTheDocument();
  });

  it("将多行商品安全摘要按原行展示", () => {
    render(<DecisionCardView card={{
      ...safeCard,
      summary: "可用于观察：可见纹理\n仅作候选：日常佩戴\n禁止对客表达：官方保真",
    }} />);

    expect(screen.getByText(/^可用于观察/)).toHaveStyle({ whiteSpace: "pre-line" });
  });

  it("将受控送回场景的完整核验理由收进可展开区域", () => {
    render(<DecisionCardView card={createDecisionCard(controlledReuseOrReturnRequest)} />);

    expect(screen.getByText("需人工复核")).toBeInTheDocument();
    expect(screen.getByText("查看全部核验项")).toBeInTheDocument();
    expect(screen.getByText(/送回资料、仓验结果及适用服务规则/)).toBeInTheDocument();
  });

  it("对风险卡明示需人工复核和一个优先核对点", () => {
    const reviewCard = createDecisionCard(controlledReuseOrReturnRequest);

    render(
      <DecisionCardView
        card={{
          ...reviewCard,
          reviewFocus: {
            field: "certificate",
            operatorPrompt: "请核对真伪或证书相关的可引用资料。",
          },
        }}
      />,
    );

    expect(screen.getByText("需人工复核")).toBeInTheDocument();
    expect(
      screen.getByText("优先核对：请核对真伪或证书相关的可引用资料。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制回复" })).toBeInTheDocument();
  });

  it("展示本次已人工确认资料的标题，而非只展示内部编号", () => {
    render(
      <DecisionCardView
        card={safeCard}
        evidenceTitles={{ "evidence-identity-1": "身份珠服务流程资料（演示）" }}
      />,
    );

    expect(screen.getByText("身份珠服务流程资料（演示）")).toBeInTheDocument();
    expect(screen.queryByText("evidence-identity-1")).toBeNull();
  });

  it("由 OPC 人工选择后生成去身份化试用记录，不自动计为经营成果", () => {
    render(<DecisionCardView card={safeCard} />);

    expect(screen.getByRole("button", { name: "生成试用记录" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("回复使用方式"), {
      target: { value: "adopted" },
    });
    fireEvent.change(screen.getByLabelText("客户当前结果"), {
      target: { value: "follow_up" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成试用记录" }));

    const record = screen.getByText(
      (_, element) =>
        element?.tagName === "PRE" &&
        element.textContent?.includes("回复使用：采纳原文") === true &&
        element.textContent?.includes("客户结果：待跟进") === true &&
        element.textContent?.includes("待人工汇总，不计入经营成果。") === true,
    );
    expect(record).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /发送/ })).toBeNull();
  });

  it("为安全草稿生成并试听本机语音", async () => {
    const voiceUrl = "blob:voice-preview";
    createObjectURLMock.mockReturnValue(voiceUrl);
    mockVoiceResponse(200, new Blob(["试听音频"], { type: "audio/mp4" }));

    render(<DecisionCardView card={safeCard} />);
    fireEvent.click(screen.getByRole("button", { name: "生成语音" }));

    expect(await screen.findByLabelText("销售回复语音试听")).toHaveAttribute("src", voiceUrl);
    expect(screen.getByRole("link", { name: "下载语音" })).toHaveAttribute(
      "download",
      "销售回复.m4a",
    );
    expect(fetchMock).toHaveBeenCalledWith("/api/voice-preview", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ card: safeCard }),
    });
  });

  it("本机女声不可用时显示中文错误，不出现播放器", async () => {
    mockVoiceResponse(503, { error: "CHINESE_FEMALE_VOICE_UNAVAILABLE" });

    render(<DecisionCardView card={safeCard} />);
    fireEvent.click(screen.getByRole("button", { name: "生成语音" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("本机未找到可用的中文女声");
    expect(screen.queryByLabelText("销售回复语音试听")).toBeNull();
  });

  it("更换或移除试听时释放旧的 Blob URL", async () => {
    createObjectURLMock.mockReturnValueOnce("blob:first").mockReturnValueOnce("blob:second");
    mockVoiceResponse(200, new Blob(["first"], { type: "audio/mp4" }));
    mockVoiceResponse(200, new Blob(["second"], { type: "audio/mp4" }));

    const { unmount } = render(<DecisionCardView card={safeCard} />);
    fireEvent.click(screen.getByRole("button", { name: "生成语音" }));
    await screen.findByLabelText("销售回复语音试听");
    fireEvent.click(screen.getByRole("button", { name: "生成语音" }));

    await waitFor(() => {
      expect(screen.getByLabelText("销售回复语音试听")).toHaveAttribute("src", "blob:second");
    });

    expect(revokeObjectURLMock).toHaveBeenCalledWith("blob:first");
    unmount();
    expect(revokeObjectURLMock).toHaveBeenCalledWith("blob:second");
  });
});
