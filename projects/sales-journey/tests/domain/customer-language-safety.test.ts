import { describe, expect, it } from "vitest";

import { createDecisionCard } from "@/domain/decision-card/advisor";
import type { DecisionCardRequest } from "@/domain/decision-card/types";
import {
  controlledReuseOrReturnRequest,
  identityBeadRequest,
  scanServiceRequest,
  swapRequest,
} from "../support/decision-card-fixtures";

type CustomerLanguageCase = {
  category: string;
  scenario: LegacyScenario;
  message: string;
  verificationField: string;
};

type LegacyScenario = "identity_bead" | "scan_service" | "swap_or_redesign" | "controlled_reuse_or_return";

const REQUEST_BY_SCENARIO: Record<LegacyScenario, DecisionCardRequest> = {
  identity_bead: identityBeadRequest,
  scan_service: scanServiceRequest,
  swap_or_redesign: swapRequest,
  controlled_reuse_or_return: controlledReuseOrReturnRequest,
};

const INFORMAL_RISK_CASES: readonly CustomerLanguageCase[] = [
  { category: "沉香", scenario: "identity_bead", message: "这串会不会是假货？", verificationField: "certificate" },
  { category: "绿松石", scenario: "identity_bead", message: "这串是不是 A 货？", verificationField: "certificate" },
  { category: "南红", scenario: "identity_bead", message: "有没有做过优化处理？", verificationField: "material" },
  { category: "和田玉", scenario: "identity_bead", message: "这个是原矿吗？", verificationField: "material" },
  { category: "彩宝", scenario: "identity_bead", message: "这颗有没有烧过？", verificationField: "material" },
  { category: "翡翠", scenario: "identity_bead", message: "有没有注胶或者染色？", verificationField: "material" },
  { category: "沉香", scenario: "scan_service", message: "可以包退吗？", verificationField: "service_policy" },
  { category: "绿松石", scenario: "scan_service", message: "最低多少能出？", verificationField: "service_policy" },
  { category: "南红", scenario: "scan_service", message: "能一口价吗？", verificationField: "service_policy" },
  { category: "和田玉", scenario: "scan_service", message: "以后还能涨吗？", verificationField: "service_policy" },
  { category: "彩宝", scenario: "swap_or_redesign", message: "改款以后你们还能收吗？", verificationField: "service_policy" },
  { category: "翡翠", scenario: "swap_or_redesign", message: "能给我保本吗？", verificationField: "service_policy" },
];

describe("客户口语化高风险提问", () => {
  it.each(INFORMAL_RISK_CASES)(
    "$category：$message 会转人工而非生成承诺",
    ({ category, scenario, message, verificationField }) => {
      const request: DecisionCardRequest = {
        ...REQUEST_BY_SCENARIO[scenario],
        requestId: `req-customer-language-${category}-${scenario}`,
        productDescription: `客户说这次卖的是${category}`,
        latestCustomerMessage: message,
      };

      const card = createDecisionCard(request);

      expect(card.deliveryMode).toBe("copy_only");
      expect(card.safety.status).toBe("reviewRequired");
      expect(card.verificationNeeded).toContainEqual(
        expect.objectContaining({ field: verificationField }),
      );
    },
  );

  it("从当前亲切对话沿用已出现的称呼生成安全承接草稿", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      requestId: "req-customer-language-warm-conversation",
      latestCustomerMessage: "这串会不会是假货？",
      conversation: [
        { speaker: "operator", text: "亲，您把图片发我，我先整理资料。" },
      ],
    });

    expect(card.deliveryMode).toBe("copy_only");
    expect(card.safety.status).toBe("reviewRequired");
    expect(card.replyOptions[0].text).toMatch(/^亲，/);
    expect(card.replyOptions[0].text).toContain("我先把您关心的这一点和现有资料整理好");
  });

  it("从当前直接对话不凭空添加称呼生成安全承接草稿", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      requestId: "req-customer-language-direct-conversation",
      latestCustomerMessage: "这串会不会是假货？",
      conversation: [
        { speaker: "operator", text: "请把图片发我，我整理资料后回复。" },
      ],
    });

    expect(card.deliveryMode).toBe("copy_only");
    expect(card.safety.status).toBe("reviewRequired");
    expect(card.replyOptions[0].text).not.toMatch(/^(亲|您好|宝|您)，/);
    expect(card.replyOptions[0].text).toContain("我先把您关心的这一点和现有资料整理好");
  });
});
