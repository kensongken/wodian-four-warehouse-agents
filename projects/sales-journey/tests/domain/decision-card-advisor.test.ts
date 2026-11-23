import { describe, expect, it } from "vitest";

import { createDecisionCard } from "@/domain/decision-card/advisor";
import type { DecisionCardRequest } from "@/domain/decision-card/types";
import {
  controlledReuseOrReturnRequest,
  identityBeadRequest,
  scanServiceRequest,
  swapRequest,
} from "../support/decision-card-fixtures";

describe("createDecisionCard", () => {
  const categoryContextCases: Array<{
    category: string;
    request: DecisionCardRequest;
    expectedSafety?: "safe" | "reviewRequired";
  }> = [
    {
      category: "沉香",
      request: {
        ...identityBeadRequest,
        requestId: "req-category-agarwood",
        evidence: [],
        productDescription: "客户说这次卖的是沉香身份珠",
        latestCustomerMessage: "第一次了解，身份珠怎么核对？",
      },
    },
    {
      category: "绿松石",
      request: {
        ...scanServiceRequest,
        requestId: "req-category-turquoise",
        productDescription: "客户说这次卖的是绿松石",
        latestCustomerMessage: "扫码后我可以先核对哪些资料？",
      },
    },
    {
      category: "南红",
      request: {
        ...swapRequest,
        requestId: "req-category-southern-red",
        productDescription: "客户说这次卖的是南红旧手串",
        latestCustomerMessage: "我想改成新款，需要准备什么？",
      },
    },
    {
      category: "和田玉",
      request: {
        ...swapRequest,
        requestId: "req-category-hetian-jade",
        productDescription: "客户说这次卖的是和田玉旧手串",
        latestCustomerMessage: "我想改款，先发哪些资料？",
      },
    },
    {
      category: "彩宝",
      request: {
        ...controlledReuseOrReturnRequest,
        requestId: "req-category-colored-gem",
        productDescription: "客户说这次卖的是彩宝旧手串",
        latestCustomerMessage: "彩宝还没仓验，可以给我固定 3000 元吗？",
      },
      expectedSafety: "reviewRequired",
    },
    {
      category: "翡翠",
      request: {
        ...controlledReuseOrReturnRequest,
        requestId: "req-category-jadeite",
        productDescription: "客户说这次卖的是翡翠旧手串",
        latestCustomerMessage: "翡翠送回仓验前要准备什么资料？",
      },
      expectedSafety: "reviewRequired",
    },
  ];

  it.each(categoryContextCases)(
    "只把客户提及的$category作为上下文，不作未经核验的断言",
    ({ category, request, expectedSafety = "safe" }) => {
      const card = createDecisionCard({
        ...request,
        conversation: [
          ...request.conversation,
          { speaker: "customer", text: `我想问${category}的情况。` },
        ],
      });
      const text = `${card.replyOptions[0].text}\n${card.nextAction.text}`;

      expect(card.deliveryMode).toBe("copy_only");
      expect(text).toContain(`您提到的${category}`);
      expect(text).not.toMatch(/产地|保值|升值|回购|置换|固定价|报价/);
      expect(card.safety.status).toBe(expectedSafety);
    },
  );

  it("安全卡不把商品描述中的品类归为客户自述", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      productDescription: "沉香身份珠",
      latestCustomerMessage: "身份珠怎么核对？",
      conversation: [{ speaker: "operator", text: "您好，我先帮您整理。" }],
    });

    expect(card.safety.status).toBe("safe");
    expect(card.replyOptions[0].text).not.toContain("就您提到的沉香");
  });

  it("客户最后一句提及品类时保留该语境", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      productDescription: "身份珠",
      latestCustomerMessage: "沉香身份珠之后你们还能收吗？",
    });

    expect(card.replyOptions[0].text).toContain("就您提到的沉香");
  });

  it("客户历史发言提及品类时在安全卡保留该语境", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      productDescription: "身份珠",
      latestCustomerMessage: "身份珠怎么核对？",
      conversation: [{ speaker: "customer", text: "我想问沉香身份珠。" }],
    });

    expect(card.safety.status).toBe("safe");
    expect(card.replyOptions[0].text).toContain("就您提到的沉香");
  });

  it("仅在引用已批准身份珠资料时说明身份珠", () => {
    const card = createDecisionCard(identityBeadRequest);

    expect(card.deliveryMode).toBe("copy_only");
    expect(card.replyOptions[0].text).toContain("身份珠");
    expect(card.replyOptions[0].evidenceIds).toEqual(["evidence-identity-1"]);
    expect(card.safety.status).toBe("safe");
  });

  it("缺少身份珠资料时只收集关注点", () => {
    const card = createDecisionCard({ ...identityBeadRequest, evidence: [] });

    expect(card.replyOptions[0].text).not.toContain("身份珠的作用");
    expect(card.replyOptions[0].evidenceIds).toEqual([]);
    expect(card.nextAction.kind).toBe("ask_clarifying_question");
  });

  it("不把扫码结果作为唯一依据", () => {
    const card = createDecisionCard(scanServiceRequest);

    expect(card.replyOptions[0].text).toContain("扫码信息可以作为核对资料的一步");
    expect(card.replyOptions[0].text).toContain("不是单独的最终判断依据");
    expect(card.replyOptions[0].text).not.toContain("扫码就能证明真伪");
    expect(card.nextAction.kind).toBe("share_verified_service_path");
  });

  it("扫码资料不足时转为人工收集资料", () => {
    const card = createDecisionCard({ ...scanServiceRequest, evidence: [] });

    expect(card.customerIntent).toBe("needs_human_review");
    expect(card.nextAction.kind).toBe("ask_clarifying_question");
    expect(card.safety.status).toBe("reviewRequired");
  });

  it("改款场景收集原物图片与偏好", () => {
    const card = createDecisionCard(swapRequest);

    expect(card.nextAction.kind).toBe("ask_clarifying_question");
    expect(card.nextAction.text).toContain("原物图片");
    expect(card.nextAction.text).toContain("尺寸");
    expect(card.nextAction.text).toContain("喜欢的风格");
    expect(card.nextAction.text).toContain("预算范围");
  });

  it("受控再利用或送回始终收集复核资料并转人工", () => {
    const card = createDecisionCard(controlledReuseOrReturnRequest);

    expect(card.nextAction.kind).toBe("collect_review_material");
    expect(card.safety.status).toBe("reviewRequired");
    expect(card.safety.rules).toEqual(
      expect.arrayContaining([
        "REUSE_OR_RETURN_NO_FIXED_PRICE",
        "REUSE_OR_RETURN_HUMAN_REVIEW",
      ]),
    );
    expect(card.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "reuse_or_return_terms" }),
    );
  });

  it("受控再利用或送回用安全承接草稿保留仓验和人工确认下一步", () => {
    const card = createDecisionCard(controlledReuseOrReturnRequest);
    const text = `${card.replyOptions[0].text}\n${card.nextAction.text}`;

    expect(card.replyOptions[0].text).toContain("我先把您关心的这一点和现有资料整理好");
    expect(text).toContain("送回仓库核验");
    expect(text).toContain("人工确认");
    expect(text).not.toMatch(/回购|置换|固定价|保底价|金额|报价|平台|到账/);
  });

  it("同一请求稳定生成同一张卡片", () => {
    expect(createDecisionCard(scanServiceRequest)).toEqual(
      createDecisionCard(scanServiceRequest),
    );
  });

  it("按代购风格添加称呼并限制回复长度", () => {
    const maxReplyCharacters = 180;
    const card = createDecisionCard({
      ...scanServiceRequest,
      operatorStyle: {
        tone: "direct",
        maxReplyCharacters,
        preferredGreeting: "您好",
      },
    });

    expect(card.replyOptions[0].text).toMatch(/^您好，/);
    expect(card.replyOptions[0].text.length).toBeLessThanOrEqual(maxReplyCharacters);
  });

  it("从当前代购对话沿用亲并遵守默认长度限制", () => {
    const card = createDecisionCard({
      ...scanServiceRequest,
      conversation: [
        { speaker: "operator", text: "亲，您把页面截图发我，我先帮您整理。" },
      ],
    });

    expect(card.replyOptions[0].text).toMatch(/^亲，/);
    expect(card.replyOptions[0].text.length).toBeLessThanOrEqual(180);
  });

  it("安全卡不会从仅含直接代购话术的对话注入称呼", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      conversation: [
        { speaker: "operator", text: "把页面截图发我，我先帮您整理。" },
      ],
    });

    expect(card.safety.status).toBe("safe");
    expect(card.replyOptions[0].text).not.toMatch(/^(亲|您好|宝|您)，/);
  });

  it("不会把仅由代购提及的品类写成客户自述", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      latestCustomerMessage: "身份珠怎么核对？",
      conversation: [
        { speaker: "operator", text: "沉香的资料我先帮您整理。" },
      ],
    });

    expect(card.safety.status).toBe("safe");
    expect(card.replyOptions[0].text).not.toContain("就您提到的沉香");
  });

  it("长度不足时保留完整的安全承接回退", () => {
    const maxReplyCharacters = 12;
    const card = createDecisionCard({
      ...controlledReuseOrReturnRequest,
      operatorStyle: {
        tone: "direct",
        maxReplyCharacters,
        preferredGreeting: "亲",
      },
    });

    expect(card.replyOptions[0].text).toBe("请人工核验后再确认。");
    expect(card.replyOptions[0].text.length).toBeLessThanOrEqual(maxReplyCharacters);
  });

  it("不把不安全的配置称呼写入回复", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      operatorStyle: {
        tone: "warm",
        maxReplyCharacters: 180,
        preferredGreeting: "天然沉香",
      },
    });

    expect(card.replyOptions[0].text).not.toContain("天然沉香");
    expect(card.safety.status).toBe("safe");
  });
});
