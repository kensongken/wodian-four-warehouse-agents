import { describe, expect, it } from "vitest";

import { resolveOperatorStyle } from "@/domain/decision-card/style";

describe("resolveOperatorStyle", () => {
  it("从代购最近的聊天中保留常用称呼", () => {
    expect(
      resolveOperatorStyle({
        conversation: [
          { speaker: "operator", text: "亲，您把图片发我，我马上帮您整理。" },
        ],
      }),
    ).toMatchObject({ tone: "warm", preferredGreeting: "亲" });
  });

  it("没有代购聊天时使用温和的默认风格", () => {
    expect(resolveOperatorStyle({ conversation: [] })).toEqual({
      tone: "warm",
      maxReplyCharacters: 180,
    });
  });

  it("忽略配置中带有未经核验事实的称呼", () => {
    expect(
      resolveOperatorStyle({
        conversation: [],
        operatorStyle: {
          tone: "warm",
          maxReplyCharacters: 180,
          preferredGreeting: "天然沉香",
        },
      }),
    ).toEqual({ tone: "warm", maxReplyCharacters: 180 });
  });

  it("为完整的安全回退保留最小回复长度", () => {
    expect(
      resolveOperatorStyle({
        conversation: [],
        operatorStyle: {
          tone: "direct",
          maxReplyCharacters: 1,
        },
      }),
    ).toEqual({ tone: "direct", maxReplyCharacters: 10 });
  });
});
