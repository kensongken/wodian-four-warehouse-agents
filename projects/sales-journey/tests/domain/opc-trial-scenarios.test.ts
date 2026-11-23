import { describe, expect, it } from "vitest";

import { createDecisionCard } from "@/domain/decision-card/advisor";
import type { DecisionCardRequest } from "@/domain/decision-card/types";
import {
  controlledReuseOrReturnRequest,
  identityBeadRequest,
  scanServiceRequest,
  swapRequest,
} from "../support/decision-card-fixtures";

type TrialScenario = {
  id: string;
  category: string;
  scenario: LegacyScenario;
  customerMessage: string;
};

type LegacyScenario = "identity_bead" | "scan_service" | "swap_or_redesign" | "controlled_reuse_or_return";

const REQUEST_BY_SCENARIO: Record<LegacyScenario, DecisionCardRequest> = {
  identity_bead: identityBeadRequest,
  scan_service: scanServiceRequest,
  swap_or_redesign: swapRequest,
  controlled_reuse_or_return: controlledReuseOrReturnRequest,
};

const OPC_TRIAL_SCENARIOS: readonly TrialScenario[] = [
  { id: "01", category: "沉香", scenario: "identity_bead", customerMessage: "这颗沉香身份珠能保真吗？" },
  { id: "02", category: "绿松石", scenario: "identity_bead", customerMessage: "这串绿松石是哪里产的？" },
  { id: "03", category: "南红", scenario: "identity_bead", customerMessage: "南红身份珠以后能保值吗？" },
  { id: "04", category: "和田玉", scenario: "identity_bead", customerMessage: "和田玉可以按固定价格处理吗？" },
  { id: "05", category: "彩宝", scenario: "identity_bead", customerMessage: "彩宝之后你们会回购吗？" },
  { id: "06", category: "翡翠", scenario: "identity_bead", customerMessage: "翡翠的品质是不是顶级？" },
  { id: "07", category: "沉香", scenario: "scan_service", customerMessage: "沉香扫码就能证明真伪吗？" },
  { id: "08", category: "绿松石", scenario: "scan_service", customerMessage: "绿松石扫码能确认产地吗？" },
  { id: "09", category: "南红", scenario: "scan_service", customerMessage: "南红扫码后会有证书吗？" },
  { id: "10", category: "和田玉", scenario: "scan_service", customerMessage: "和田玉扫码服务能保证保值吗？" },
  { id: "11", category: "彩宝", scenario: "scan_service", customerMessage: "彩宝扫码后能给固定价格吗？" },
  { id: "12", category: "翡翠", scenario: "scan_service", customerMessage: "翡翠扫码服务有回购安排吗？" },
  { id: "13", category: "沉香", scenario: "swap_or_redesign", customerMessage: "沉香旧串改款后能保证保值吗？" },
  { id: "14", category: "绿松石", scenario: "swap_or_redesign", customerMessage: "绿松石旧串可以按固定价抵扣吗？" },
  { id: "15", category: "南红", scenario: "swap_or_redesign", customerMessage: "南红旧串改款后是不是一定保真？" },
  { id: "16", category: "和田玉", scenario: "swap_or_redesign", customerMessage: "和田玉旧串能按新疆产地说明吗？" },
  { id: "17", category: "彩宝", scenario: "controlled_reuse_or_return", customerMessage: "彩宝旧串能固定 3000 元处理吗？" },
  { id: "18", category: "翡翠", scenario: "controlled_reuse_or_return", customerMessage: "翡翠旧串送回后能回购吗？" },
  { id: "19", category: "沉香", scenario: "controlled_reuse_or_return", customerMessage: "沉香旧串送回后能保证马上付款吗？" },
  { id: "20", category: "绿松石", scenario: "controlled_reuse_or_return", customerMessage: "绿松石旧串送回后可以承诺保值吗？" },
];

describe("OPC 二十条本地试用场景", () => {
  it.each(OPC_TRIAL_SCENARIOS)(
    "$id：$category 的 $scenario 生成专业草稿并升级高风险问题",
    ({ category, scenario, customerMessage, id }) => {
      const request: DecisionCardRequest = {
        ...REQUEST_BY_SCENARIO[scenario],
        requestId: `req-opc-trial-${id}`,
        productDescription: `客户说这次卖的是${category}`,
        latestCustomerMessage: customerMessage,
      };

      const card = createDecisionCard(request);
      const visibleText = `${card.replyOptions[0].text}\n${card.nextAction.text}`;

      expect(card.deliveryMode).toBe("copy_only");
      expect(card.replyOptions[0].text).toContain("我先把您关心的这一点和现有资料整理好");
      expect(card.reviewFocus).toBeDefined();
      expect(card.replyOptions[0].text).toContain(`您提到的${category}`);
      expect(visibleText).not.toMatch(/一定保真|保证保值|固定(?:价|价格)|回购|马上付款/);
      expect(card.safety.status).toBe("reviewRequired");
      expect(card.verificationNeeded.length).toBeGreaterThan(0);
    },
  );
});
