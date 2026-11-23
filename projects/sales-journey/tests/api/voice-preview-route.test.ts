import { afterEach, describe, expect, it, vi } from "vitest";

import { VoicePreviewError } from "@/server/voice/local-speech";
import { safeCard } from "../support/decision-card-fixtures";

const voice = vi.hoisted(() => ({
  synthesizeLocalPreview: vi.fn(),
}));

vi.mock("@/server/voice/local-speech", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/server/voice/local-speech")>();
  return { ...actual, synthesizeLocalPreview: voice.synthesizeLocalPreview };
});

import { POST } from "@/app/api/voice-preview/route";

const preview = {
  audio: new Uint8Array([1, 2, 3]),
  contentType: "audio/mp4" as const,
  fileName: "销售回复.m4a",
  downloadable: true,
};

function jsonRequest(body: unknown) {
  return new Request("http://localhost/api/voice-preview", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("POST /api/voice-preview", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("只用 copy_only 卡片的第一条草稿返回试听音频", async () => {
    voice.synthesizeLocalPreview.mockResolvedValue(preview);
    const secondReply = {
      ...safeCard.replyOptions[0],
      id: "reply-2",
      text: "这条草稿不能用于语音。",
    };

    const response = await POST(
      jsonRequest({
        card: { ...safeCard, replyOptions: [safeCard.replyOptions[0], secondReply] },
      }),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("audio/mp4");
    expect(response.headers.get("x-voice-downloadable")).toBe("true");
    expect(response.headers.get("content-disposition")).toBe(
      "inline; filename=\"sales-reply.m4a\"; filename*=UTF-8''%E9%94%80%E5%94%AE%E5%9B%9E%E5%A4%8D.m4a",
    );
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));
    expect(voice.synthesizeLocalPreview).toHaveBeenCalledWith(safeCard.replyOptions[0].text, "warm");
  });

  it("拒绝自由文本字段而不调用语音合成", async () => {
    const response = await POST(
      jsonRequest({
        card: safeCard,
        text: "这段任意用户输入绝不能进入语音合成。",
      }),
    );

    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ error: "INVALID_VOICE_PREVIEW_REQUEST" });
    expect(voice.synthesizeLocalPreview).not.toHaveBeenCalled();
  });

  it("拒绝非 copy_only 卡片而不调用语音合成", async () => {
    const response = await POST(
      jsonRequest({ card: { ...safeCard, deliveryMode: "send_now" } }),
    );

    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ error: "INVALID_VOICE_PREVIEW_REQUEST" });
    expect(voice.synthesizeLocalPreview).not.toHaveBeenCalled();
  });

  it("拒绝无效 JSON 而不调用语音合成", async () => {
    const response = await POST(
      new Request("http://localhost/api/voice-preview", { method: "POST", body: "not-json" }),
    );

    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ error: "INVALID_VOICE_PREVIEW_REQUEST" });
    expect(voice.synthesizeLocalPreview).not.toHaveBeenCalled();
  });

  it("没有本机中文女声时返回 503 中文错误", async () => {
    voice.synthesizeLocalPreview.mockRejectedValue(
      new VoicePreviewError("CHINESE_FEMALE_VOICE_UNAVAILABLE"),
    );

    const response = await POST(jsonRequest({ card: safeCard }));

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error: "CHINESE_FEMALE_VOICE_UNAVAILABLE",
      message: "本机未找到可用的中文女声。",
    });
  });

  it("语音生成失败时返回 500 结构化错误", async () => {
    voice.synthesizeLocalPreview.mockRejectedValue(new VoicePreviewError("VOICE_GENERATION_FAILED"));

    const response = await POST(jsonRequest({ card: safeCard }));

    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({
      error: "VOICE_GENERATION_FAILED",
      message: "本机未找到可用的中文女声。",
    });
  });
});
